use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use ed25519_dalek::{Signer as _, SigningKey, pkcs8::DecodePrivateKey};
use rand::RngCore;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
use zeroize::{Zeroize, Zeroizing};

pub struct Signer {
    key: SigningKey,
    key_id: String,
}

impl Signer {
    pub fn load(home: &Path) -> Result<Self, String> {
        // Match auth.client_key_path without resolving redirected Windows AppData.
        let absolute = std::path::absolute(home).map_err(|_| "Cannot locate signing identity")?;
        let identity = absolute.to_string_lossy().replace('/', "\\").to_lowercase();
        let instance = hex::encode(Sha256::digest(identity.as_bytes()));
        let user = std::env::var_os("USERPROFILE").ok_or("Cannot locate client key store")?;
        let path = Path::new(&user)
            .join(".codex-permissive-clients")
            .join(&instance[..16])
            .join("api/private-key.dpapi");
        let protected = Zeroizing::new(
            std::fs::read(path).map_err(|_| "Cannot read the protected API signing key")?,
        );
        let plain = unprotect(&protected)?;
        let pem = std::str::from_utf8(&plain).map_err(|_| "Invalid API signing identity")?;
        let key = SigningKey::from_pkcs8_pem(pem).map_err(|_| "Invalid API signing identity")?;
        Ok(Self::new(key))
    }

    fn new(key: SigningKey) -> Self {
        let key_id = hex::encode(Sha256::digest(key.verifying_key().as_bytes()));
        Self { key, key_id }
    }

    pub fn headers(
        &self,
        method: &str,
        target: &str,
        body: &[u8],
    ) -> Result<BTreeMap<String, String>, String> {
        let created = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "Invalid system clock")?
            .as_secs();
        let mut nonce = [0_u8; 24];
        rand::rngs::OsRng.fill_bytes(&mut nonce);
        self.headers_at(
            method,
            target,
            body,
            created,
            &URL_SAFE_NO_PAD.encode(nonce),
        )
    }

    fn headers_at(
        &self,
        method: &str,
        target: &str,
        body: &[u8],
        created: u64,
        nonce: &str,
    ) -> Result<BTreeMap<String, String>, String> {
        if !target.starts_with('/')
            || target.starts_with("//")
            || !target.is_ascii()
            || target.contains(['\r', '\n', '#'])
        {
            return Err("Invalid signed API route".into());
        }
        let (path, query) = target.split_once('?').unwrap_or((target, ""));
        let mut headers = BTreeMap::new();
        let digest = format!("sha-256=:{}:", STANDARD.encode(Sha256::digest(body)));
        headers.insert("content-digest".into(), digest.clone());
        let mut fields = vec!["@method", "@path", "@query", "content-digest"];
        if !body.is_empty() {
            fields.push("content-type");
            headers.insert("content-type".into(), "application/json".into());
        }
        let parameters = format!(
            "({});created={created};expires={};nonce=\"{nonce}\";keyid=\"{}\";alg=\"ed25519\"",
            fields
                .iter()
                .map(|field| format!("\"{field}\""))
                .collect::<Vec<_>>()
                .join(" "),
            created + 60,
            self.key_id
        );
        let mut base = format!(
            "\"@method\": {}\n\"@path\": {path}\n\"@query\": ?{query}\n\"content-digest\": {digest}",
            method.to_uppercase()
        );
        if !body.is_empty() {
            base.push_str("\n\"content-type\": application/json");
        }
        base.push_str(&format!("\n\"@signature-params\": {parameters}"));
        let signature = self.key.sign(base.as_bytes());
        headers.insert("signature-input".into(), format!("permissive={parameters}"));
        headers.insert(
            "signature".into(),
            format!("permissive=:{}:", STANDARD.encode(signature.to_bytes())),
        );
        Ok(headers)
    }
}

#[cfg(windows)]
fn unprotect(value: &[u8]) -> Result<Zeroizing<Vec<u8>>, String> {
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::Cryptography::{
            CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN, CryptUnprotectData,
        },
    };
    let source = CRYPT_INTEGER_BLOB {
        cbData: value
            .len()
            .try_into()
            .map_err(|_| "Invalid protected signing key")?,
        pbData: value.as_ptr().cast_mut(),
    };
    let mut target = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: std::ptr::null_mut(),
    };
    // DPAPI input is borrowed; output is owned by LocalAlloc and freed below.
    unsafe {
        if CryptUnprotectData(
            &source,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut target,
        ) == 0
        {
            return Err("Cannot unlock the protected API signing key".into());
        }
        let result = Zeroizing::new(
            std::slice::from_raw_parts(target.pbData, target.cbData as usize).to_vec(),
        );
        std::slice::from_raw_parts_mut(target.pbData, target.cbData as usize).zeroize();
        LocalFree(target.pbData.cast());
        Ok(result)
    }
}

#[cfg(not(windows))]
fn unprotect(value: &[u8]) -> Result<Zeroizing<Vec<u8>>, String> {
    Ok(Zeroizing::new(value.to_vec()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, Verifier};

    #[test]
    fn signature_covers_method_encoded_route_query_and_body() {
        let signer = Signer::new(SigningKey::from_bytes(&[7; 32]));
        let headers = signer
            .headers_at(
                "POST",
                "/v1/tasks?workspace=one%20two",
                br#"{"prompt":"PRIVATE-PROMPT"}"#,
                123,
                "abcdefghijklmnopqrstuvwx",
            )
            .unwrap();
        let digest = &headers["content-digest"];
        let parameters = headers["signature-input"]
            .strip_prefix("permissive=")
            .unwrap();
        let base = format!(
            "\"@method\": POST\n\"@path\": /v1/tasks\n\"@query\": ?workspace=one%20two\n\"content-digest\": {digest}\n\"content-type\": application/json\n\"@signature-params\": {parameters}"
        );
        let raw = headers["signature"]
            .strip_prefix("permissive=:")
            .unwrap()
            .strip_suffix(':')
            .unwrap();
        let signature = Signature::from_slice(&STANDARD.decode(raw).unwrap()).unwrap();
        signer
            .key
            .verifying_key()
            .verify(base.as_bytes(), &signature)
            .unwrap();
        assert!(!headers.values().any(|value| value.contains("PRIVATE-")));
        assert!(
            signer
                .key
                .verifying_key()
                .verify(base.replace("POST", "GET").as_bytes(), &signature)
                .is_err()
        );
    }

    #[test]
    fn fresh_nonces_and_route_validation() {
        let signer = Signer::new(SigningKey::from_bytes(&[8; 32]));
        assert_ne!(
            signer.headers("GET", "/v1/health", b"").unwrap()["signature-input"],
            signer.headers("GET", "/v1/health", b"").unwrap()["signature-input"]
        );
        assert!(
            signer
                .headers("GET", "https://outside.test/PRIVATE-PROMPT", b"")
                .is_err()
        );
    }
}
