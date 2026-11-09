use serde_json::Value;

// SSE frames are decoded only after all bytes arrive, including split UTF-8.
#[derive(Default)]
pub struct EventDecoder {
    pending: Vec<u8>,
}
impl EventDecoder {
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<Value>, String> {
        self.pending.extend_from_slice(bytes);
        if self.pending.len() > 8 * 1024 * 1024 {
            return Err("Task event exceeds the desktop limit".into());
        }
        let mut events = Vec::new();
        loop {
            let lf = self
                .pending
                .windows(2)
                .position(|value| value == b"\n\n")
                .map(|pos| (pos, 2));
            let crlf = self
                .pending
                .windows(4)
                .position(|value| value == b"\r\n\r\n")
                .map(|pos| (pos, 4));
            let next = match (lf, crlf) {
                (Some(a), Some(b)) => Some(if a.0 < b.0 { a } else { b }),
                (a, b) => a.or(b),
            };
            let Some((end, size)) = next else { break };
            let frame = std::str::from_utf8(&self.pending[..end])
                .map_err(|_| "Invalid task event encoding")?;
            let data = frame
                .lines()
                .filter_map(|line| line.strip_prefix("data:"))
                .map(|line| line.strip_prefix(' ').unwrap_or(line))
                .collect::<Vec<_>>()
                .join("\n");
            if !data.is_empty() {
                events
                    .push(serde_json::from_str(&data).map_err(|_| "Invalid task event response")?);
            }
            self.pending.drain(..end + size);
        }
        Ok(events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn handles_split_unicode_crlf_keepalives_and_multiple_data_lines() {
        let wire = ": connected\r\n\r\ndata: {\r\ndata: \"text\":\"héllo PRIVATE-PROMPT\"}\r\n\r\n"
            .as_bytes();
        let mut decoder = EventDecoder::default();
        let mut events = Vec::new();
        for byte in wire {
            events.extend(decoder.push(&[*byte]).unwrap());
        }
        assert_eq!(
            events,
            vec![serde_json::json!({"text": "héllo PRIVATE-PROMPT"})]
        );
    }
    #[test]
    fn malformed_payload_does_not_enter_error_message() {
        assert_eq!(
            EventDecoder::default()
                .push(b"data: PRIVATE-PROMPT\n\n")
                .unwrap_err(),
            "Invalid task event response"
        );
    }
}
