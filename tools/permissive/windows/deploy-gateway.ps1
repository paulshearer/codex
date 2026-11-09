param([string]$Target = 'dgx-spark')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& ssh.exe -o BatchMode=yes -o StrictHostKeyChecking=yes $Target 'mkdir -p /home/pshearer/codex-permissive-gateway'
if ($LASTEXITCODE) { throw 'Gateway staging failed.' }
& scp.exe (Join-Path $root 'permissive/sparky_gateway.py') "${Target}:/home/pshearer/codex-permissive-gateway/gateway.py"
if ($LASTEXITCODE) { throw 'Gateway upload failed.' }
$script = @'
set -eu
sudo -n python3 -m venv /opt/codex-permissive-gateway
sudo -n /opt/codex-permissive-gateway/bin/pip install aiohttp==3.13.3 >/dev/null
sudo -n install -m 0644 /home/pshearer/codex-permissive-gateway/gateway.py /opt/codex-permissive-gateway/gateway.py
sudo -n tee /etc/systemd/system/codex-permissive-gateway.service >/dev/null <<'UNIT'
[Unit]
Description=Codex Permissive inference gateway
After=network-online.target docker.service
[Service]
ExecStart=/opt/codex-permissive-gateway/bin/python /opt/codex-permissive-gateway/gateway.py --proxy-directory /home/pshearer/deploy/deepseek-v41-flash-tp3/state/reliability-20260912/proxy
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now codex-permissive-gateway.service
sudo -n systemctl restart codex-permissive-gateway.service
sudo -n tailscale serve --bg --https=8445 http://127.0.0.1:8003
curl --fail --silent --show-error --retry 8 --retry-connrefused --retry-delay 2 http://127.0.0.1:8003/v1/models
'@
try {
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('permissive-gateway-' + [Guid]::NewGuid() + '.sh')
    [IO.File]::WriteAllText($temporary, $script.Replace("`r`n", "`n") + "`n", [Text.UTF8Encoding]::new($false))
    & scp.exe $temporary "${Target}:/home/pshearer/codex-permissive-gateway/deploy.sh"
    if ($LASTEXITCODE) { throw 'Gateway deployment script upload failed.' }
    & ssh.exe -o BatchMode=yes -o StrictHostKeyChecking=yes $Target 'bash /home/pshearer/codex-permissive-gateway/deploy.sh'
    if ($LASTEXITCODE) { throw 'Gateway deployment failed.' }
} finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary }
    & ssh.exe -o BatchMode=yes -o StrictHostKeyChecking=yes $Target 'sudo -n rm -f /etc/sudoers.d/99-pshearer-codex-temp; sudo -k'
}
