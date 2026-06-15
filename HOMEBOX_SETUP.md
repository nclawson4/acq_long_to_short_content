# Home box SSH key install

Paste this whole block into the **Administrator: Windows PowerShell** window on the home box (`100.113.83.54`). It appends my public key to `authorized_keys` and locks down the file ACLs the way Windows OpenSSH requires.

```powershell
$pub = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILJUeAdftr+JtQItPH35ZnjloRxLMyi/Zu6gl2NKemXV acq-clipper-vercel-ingest'
$sshDir = "$env:USERPROFILE\.ssh"
if (!(Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir | Out-Null }
$authFile = "$sshDir\authorized_keys"
Add-Content -Path $authFile -Value $pub
icacls $authFile /inheritance:r /grant "$($env:USERNAME):F" "SYSTEM:F" /remove "Authenticated Users" "Users" 2>$null
Get-Content $authFile | Measure-Object -Line
```

The last line prints the line count of `authorized_keys` — that's how we confirm the key landed. Paste the output back into chat.

---

## Step 2 — install the key in the admin location (Windows quirk)

Windows OpenSSH ignores `~/.ssh/authorized_keys` for admin users and only reads `C:\ProgramData\ssh\administrators_authorized_keys`. Paste this in the same Admin PowerShell:

```powershell
$pub = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILJUeAdftr+JtQItPH35ZnjloRxLMyi/Zu6gl2NKemXV acq-clipper-vercel-ingest'
$adminAuth = 'C:\ProgramData\ssh\administrators_authorized_keys'
if (!(Test-Path $adminAuth)) { New-Item -ItemType File -Path $adminAuth -Force | Out-Null }
Add-Content -Path $adminAuth -Value $pub
icacls $adminAuth /inheritance:r /grant 'Administrators:F' 'SYSTEM:F'
Restart-Service sshd
Get-Content $adminAuth | Measure-Object -Line
```

Paste the output back. After this I should be able to SSH in.

