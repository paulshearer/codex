"""Exercise host boundary generation without provisioning or controlling a VM."""
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "windows" / "sandbox"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell 5.1 is required")
class SandboxScripts(unittest.TestCase):
    def powershell(self, code):
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", code],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def common(self, code):
        path = str(SCRIPTS / "common.ps1").replace("'", "''")
        return self.powershell(". '" + path + "'; " + code)

    def test_all_scripts_parse_in_windows_powershell(self):
        path = str(SCRIPTS).replace("'", "''")
        output = self.powershell(
            "$failures=@(); Get-ChildItem -LiteralPath '" + path + "' -Filter '*.ps1' | ForEach-Object {"
            "$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors)|Out-Null;"
            "$failures+=@($errors|ForEach-Object {$_.Message})}; ConvertTo-Json -InputObject @($failures)"
        )
        self.assertEqual(json.loads(output), [])

    def test_subnet_overlap_handles_route_and_host_prefixes(self):
        result = self.common(
            "@((Test-SandboxPrefixOverlap '172.31.254.0/24' '172.31.254.130/32'),"
            "(Test-SandboxPrefixOverlap '172.31.254.0/30' '172.31.254.4/30'),"
            "(Test-SandboxPrefixOverlap '10.0.0.0/8' '10.255.255.255'),"
            "(Test-SandboxPrefixOverlap '1.2.3.0/24' '0.0.0.0/0'))|ConvertTo-Json"
        )
        self.assertEqual(json.loads(result), [True, False, True, True])

    def test_guest_lsa_helper_compiles_without_calling_windows_security_apis(self):
        path = str(SCRIPTS / "Initialize-Guest.ps1").replace("'", "''")
        output = self.powershell(
            "$tokens=$null; $errors=$null; $ast=[System.Management.Automation.Language.Parser]::ParseFile('" + path + "',[ref]$tokens,[ref]$errors);"
            "$code=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $n.Value.StartsWith('using System;')},$true);"
            "if (@($code).Count -ne 1) {throw 'Expected one static LSA helper'}; Add-Type -TypeDefinition $code[0].Value;"
            "[bool]([PermissiveGuestLsa].GetMethod('SetPassword'))|ConvertTo-Json"
        )
        self.assertTrue(json.loads(output))

    def test_bundle_files_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inside.json").write_text("{}")
            path = str(root).replace("'", "''")
            output = self.common(
                "$good=Get-SandboxBundleFile '" + path + "' 'inside.json';"
                "$bad=$false; try {Get-SandboxBundleFile '" + path + "' '../escape.json'|Out-Null} catch {$bad=$true};"
                "@{name=[IO.Path]::GetFileName($good);rejected=$bad}|ConvertTo-Json"
            )
            self.assertEqual(json.loads(output), {"name": "inside.json", "rejected": True})

    def rules(self):
        return json.loads(self.common(
            "$binding=[pscustomobject]@{guestIp='172.31.254.2';gatewayIp='172.31.254.1';"
            "denyPrefixes=@((Get-SandboxDenyPrefixes)) + @('8.8.4.0/24','93.184.216.34/32')};"
            "Get-SandboxAclSpecification $binding|ConvertTo-Json -Depth 8"
        ))

    def test_rule_table_enforces_destinations_and_source_identity(self):
        rules = self.rules()

        def verdict(remote, port=443, protocol="TCP", local="172.31.254.2"):
            address = ipaddress.ip_address(remote)
            for rule in sorted(rules, key=lambda item: item["Weight"], reverse=True):
                if rule["Direction"] != "Outbound":
                    continue
                prefix = rule["RemoteIPAddress"]
                if prefix != "ANY" and address not in ipaddress.ip_network(prefix):
                    continue
                if "LocalIPAddress" in rule and ipaddress.ip_address(local) not in ipaddress.ip_network(rule["LocalIPAddress"]):
                    continue
                if rule.get("Protocol", protocol) != protocol or str(rule.get("RemotePort", port)) != str(port):
                    continue
                return rule["Action"]
            return "Deny"

        for remote in ["10.0.0.1", "192.168.1.1", "100.65.50.121", "169.254.169.254", "127.0.0.1", "8.8.4.1", "93.184.216.34", "fd7a:115c:a1e0::1", "2606:4700:4700::1111"]:
            with self.subTest(remote=remote):
                self.assertEqual(verdict(remote), "Deny")
        self.assertEqual(verdict("1.1.1.1", 53, "UDP"), "Allow")
        self.assertEqual(verdict("1.1.1.1"), "Allow")
        self.assertEqual(verdict("172.31.254.1", 18445), "Allow")
        self.assertEqual(verdict("172.31.254.1", 445), "Deny")
        self.assertEqual(verdict("172.31.254.1", 18445, "UDP"), "Deny")
        self.assertEqual(verdict("1.1.1.1", local="172.31.254.3"), "Deny")
        self.assertEqual(verdict("1.1.1.1", local="192.168.1.171"), "Deny")
        allows = [rule for rule in rules if rule["Action"] == "Allow"]
        self.assertTrue(all(rule["Stateful"] for rule in allows))
        self.assertTrue(all(rule["Direction"] == "Outbound" for rule in allows))
        self.assertEqual(len(allows), 3)

    def test_untrusted_request_is_stdin_data_in_fixed_guest_program(self):
        source = (SCRIPTS / "Invoke-SandboxCommand.ps1").read_text()
        self.assertIn("[string]$RequestPath", source)
        self.assertIn("$start.Arguments = '-m permissive.guest_bridge'", source)
        self.assertIn("StandardInput.BaseStream.Write", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("-Command $", source)

    def test_live_isolation_helper_is_fixed_bounded_and_restores_owned_changes(self):
        source = (SCRIPTS / 'Test-SandboxIsolation.ps1').read_text()
        self.assertIn('[ValidateRange(2000,3000)]', source)
        self.assertIn('Test-SandboxPerimeter $binding', source)
        self.assertIn('Set-NetFirewallProfile -Profile Domain,Private,Public -Enabled False', source)
        self.assertIn('https://permissive-gateway.internal:18445/v1/models', source)
        self.assertIn("TcpProbe '169.254.169.254' 80", source)
        self.assertIn("TcpProbe '1.1.1.1' 443 $ProbeSpec.spoofIp", source)
        self.assertIn('-PolicyStore ActiveStore', source)
        self.assertIn('source_bind_failed', source)
        self.assertIn("'mac_spoof_mutation' 'untested'", source)
        outer_finally = source[source.index('finally {\n    if ($snapshot -and $spec)'):]
        self.assertIn('-ScriptBlock $restoreGuest -ArgumentList $snapshot,$spec', outer_finally)
        self.assertIn('$route | Remove-NetRoute -Confirm:$false', outer_finally)
        self.assertIn('Remove-NetFirewallRule -Name $name', outer_finally)
        self.assertIn('$listener.Stop()', outer_finally)
        self.assertIn("'incomplete'", outer_finally)
        for unsafe in ('Invoke-Expression', '[ScriptBlock]::Create', 'Set-VMNetworkAdapter -', 'Start-VM', 'Stop-VM'):
            self.assertNotIn(unsafe, source)

    def controller_registration(self, existing="absent"):
        """Run the real registration function while replacing all task cmdlets."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package with spaces"
            package.mkdir()
            binding_path = root / "source binding.json"
            binding_path.write_text(json.dumps({
                "schema": "codex-permissive-sandbox-binding/v1", "role": "controller",
                "sandboxId": "b87d635f-f19b-4071-af97-a654e6b0b0d4",
                "vmId": "17ed4b78-bf15-4383-8346-fc47be880970",
                "controllerHome": str(root / "history with spaces"),
            }))
            binding = str(binding_path).replace("'", "''")
            packaged = str(package).replace("'", "''")
            output = self.common(
                "$script:registered=$null; $script:existing=$null;"
                "function Get-ScheduledTask {param($TaskName,$ErrorAction) $script:existing};"
                "function New-ScheduledTaskAction {param($Execute,$Argument) [pscustomobject]@{Execute=$Execute;Arguments=$Argument}};"
                "function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel) [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}};"
                "function New-ScheduledTaskTrigger {param([switch]$AtLogOn,$User) [pscustomobject]@{AtLogOn=[bool]$AtLogOn;User=$User}};"
                "function New-ScheduledTaskSettingsSet {param($MultipleInstances,$ExecutionTimeLimit,[switch]$StartWhenAvailable,$RestartCount,$RestartInterval,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries) "
                "[pscustomobject]@{MultipleInstances=$MultipleInstances;ExecutionTimeLimitSeconds=$ExecutionTimeLimit.TotalSeconds;RestartCount=$RestartCount}};"
                "function Register-ScheduledTask {param($TaskName,$Action,$Principal,$Trigger,$Settings,[switch]$Force) "
                "$script:registered=[pscustomobject]@{TaskName=$TaskName;Action=$Action;Principal=$Principal;Trigger=$Trigger;Settings=$Settings;Force=[bool]$Force}};"
                "$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name;"
                "$scriptPath=Join-Path '" + packaged + "' 'windows\\sandbox\\Start-Controller.ps1';"
                "$arguments='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"'+$scriptPath+'\" -BindingPath \"'+[IO.Path]::GetFullPath('" + binding + "')+'\"';"
                "$mode='" + existing + "'; if ($mode -ne 'absent') {"
                "$execute=Join-Path $env:WINDIR 'System32\\WindowsPowerShell\\v1.0\\powershell.exe';"
                "$logon='Interactive'; if ($mode -eq 'wrong-executable') {$execute='C:\\unrelated.exe'}; if ($mode -eq 'wrong-logon') {$logon='ServiceAccount'};"
                "$script:existing=[pscustomobject]@{Actions=[pscustomobject]@{Execute=$execute;Arguments=$arguments};Principal=[pscustomobject]@{UserId=$identity;RunLevel='Highest';LogonType=$logon}}};"
                "Install-SandboxControllerBinding '" + binding + "' '" + packaged + "';"
                "@{identity=$identity;registered=$script:registered;copied=(Test-Path -LiteralPath (Join-Path '" + packaged + "' 'deployment.json'))}|ConvertTo-Json -Depth 8"
            )
            return json.loads(output), str(binding_path), str(package)

    def test_controller_task_is_fixed_highest_current_user_interactive(self):
        result, binding, package = self.controller_registration()
        task = result["registered"]
        self.assertTrue(result["copied"])
        self.assertEqual(task["TaskName"], "CodexPermissiveController-b87d635f-f19b-4071-af97-a654e6b0b0d4")
        self.assertEqual(task["Principal"], {"UserId": result["identity"], "LogonType": "Interactive", "RunLevel": "Highest"})
        self.assertEqual(task["Trigger"], {"AtLogOn": True, "User": result["identity"]})
        self.assertTrue(task["Action"]["Execute"].lower().endswith(r"system32\windowspowershell\v1.0\powershell.exe"))
        self.assertIn('"' + str(Path(package) / "windows" / "sandbox" / "Start-Controller.ps1") + '"', task["Action"]["Arguments"])
        self.assertIn('-BindingPath "' + binding + '"', task["Action"]["Arguments"])
        self.assertEqual(task["Settings"]["MultipleInstances"], "IgnoreNew")
        self.assertEqual(task["Settings"]["ExecutionTimeLimitSeconds"], 0)
        self.assertNotIn("guest", task["Action"]["Arguments"].lower())

    def test_controller_task_correct_binding_is_reused_but_stale_action_is_replaced(self):
        self.assertIsNone(self.controller_registration("matching")[0]["registered"])
        for mode in ["wrong-executable", "wrong-logon"]:
            with self.subTest(mode=mode):
                self.assertIsNotNone(self.controller_registration(mode)[0]["registered"])

    def test_desktop_start_triggers_only_the_fixed_preinstalled_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            binding = Path(temporary) / "binding.json"
            binding.write_text(json.dumps({"role": "controller", "sandboxId": "b87d635f-f19b-4071-af97-a654e6b0b0d4"}))
            script = str(SCRIPTS / "Start-Controller.ps1").replace("'", "''")
            path = str(binding).replace("'", "''")
            output = self.powershell(
                "$global:started=$null; function Start-ScheduledTask {param($TaskName,$ErrorAction) $global:started=$TaskName};"
                "function Import-Module {throw 'Desktop startup tried to import privileged VM operations'};"
                "function Invoke-Command {throw 'Desktop startup tried to execute guest code'};"
                "$before=(Get-Location).Path; & '" + script + "' -BindingPath '" + path + "' -StartTask;"
                "@{task=$global:started;locationUnchanged=((Get-Location).Path -eq $before)}|ConvertTo-Json"
            )
            self.assertEqual(json.loads(output), {"task": "CodexPermissiveController-b87d635f-f19b-4071-af97-a654e6b0b0d4", "locationUnchanged": True})
        source = (SCRIPTS / "Start-Controller.ps1").read_text()
        self.assertIn("serve --role controller", source)
        self.assertIn("$binding.controllerHome", source)
        self.assertNotIn("serve --role guest", source)
        self.assertNotIn("codex-app-server", source)
        self.assertNotIn("desktop-agent.exe", source)

    def test_service_launchers_ensure_local_identity_before_serving(self):
        for name, role in (("Start-GuestRunner.ps1", "guest"), ("Start-Controller.ps1", "controller")):
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text()
                commands = source.splitlines()
                identity = [line for line in commands if "-m permissive.auth " in line]
                self.assertEqual(len(identity), 1)
                self.assertIn("--home $env:PERMISSIVE_HOME init-local --ensure", identity[0])
                start = source.index("serve --role " + role)
                initialized = source.index(identity[0])
                guard = source.index("if ($LASTEXITCODE)", initialized)
                self.assertLess(initialized, guard)
                self.assertLess(guard, start)
                self.assertIn("throw ", source[guard:start])
                self.assertLess(source.index("$env:PERMISSIVE_HOME ="), initialized)

    def test_portable_import_explicitly_initializes_destination_before_start(self):
        for name in ("Import-Sandbox.ps1", "New-Sandbox.ps1"):
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text()
                imported = source.index("-m permissive.portability --import-state")
                identity = source.index("-m permissive.auth ", imported)
                line = source[identity:source.index("\n", identity)]
                self.assertIn(" init-local", line)
                self.assertNotIn("--ensure", line)
                start = source.index("Start-VM", identity)
                complete = source.index("'Complete-Sandbox.ps1'", identity)
                import_guard = source.index("if ($LASTEXITCODE)", imported)
                identity_guard = source.index("if ($LASTEXITCODE)", identity)
                self.assertLess(import_guard, identity)
                self.assertLess(identity_guard, start)
                self.assertLess(identity_guard, complete)
                self.assertIn("throw ", source[identity_guard:start])

    def test_offline_boot_and_cold_import_remain_disconnected_until_verified(self):
        source = (SCRIPTS / "New-Sandbox.ps1").read_text()
        self.assertIn("-Generation 2 -Version '9.0' -MemoryStartupBytes 16GB", source)
        self.assertIn("-SizeBytes 160GB", source)
        self.assertIn("-ProcessorCount 8", source)
        self.assertIn("-IsoSha256", (SCRIPTS / "README.md").read_text())
        complete = (SCRIPTS / "Complete-Sandbox.ps1").read_text()
        self.assertLess(complete.index("Test-SandboxPerimeter"), complete.index("Connect-VMNetworkAdapter"))
        self.assertIn("Disconnect-VMNetworkAdapter", complete[complete.index("} catch {"):])
        imported = (SCRIPTS / "Import-Sandbox.ps1").read_text()
        self.assertIn("-Copy -GenerateNewId", imported)
        self.assertIn("An unrelated WinNAT exists", imported)
        self.assertNotIn("Remove-NetNat", imported)


class StartupIdentity(unittest.TestCase):
    def test_ensure_keeps_revocation_without_loading_or_generating_private_keys(self):
        from permissive import auth

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            private = Path(temporary) / "client.pem"
            public = private.with_suffix(".pub")
            generated = auth.generate(private, public)
            auth.authorize(home, public.read_bytes(), "Revoked local API client")
            auth.revoke(home, generated["keyId"])
            before = auth.registry_path(home).read_bytes()
            with patch.object(auth, "migrate_local_clients"), \
                    patch.object(auth, "generate", side_effect=AssertionError("Startup generated a new identity")), \
                    patch.object(auth, "Signer", side_effect=AssertionError("Startup loaded a private identity")):
                self.assertEqual(auth.initialize_local(home, ensure=True), {"initialized": True})
            self.assertEqual(auth.registry(home)["keys"], [])
            self.assertEqual(auth.registry_path(home).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
