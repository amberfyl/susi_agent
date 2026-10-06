# SSH read-only inventory before host-key remediation

When the user asks to "first check what I currently have" before fixing host-key conflicts, do a read-only SSH inventory pass before any remediation.

Checklist (read-only)
1) Enumerate local SSH material under `~/.ssh` (keys, known_hosts, optional config).
2) For each target host involved, query existing host-key entries with:
   - `ssh-keygen -F <host> -f ~/.ssh/known_hosts`
3) Show current public-key fingerprint to confirm which identity is in use:
   - `ssh-keygen -lf ~/.ssh/id_ed25519.pub`
4) Report facts only:
   - host has known_hosts entry vs no entry
   - line number if present
   - whether `~/.ssh/config` exists
   - active key fingerprint/comment

Hard guard
- Do NOT modify `known_hosts` (`ssh-keygen -R`, edits, rewrites) in this review step.
- Do NOT run remote side-effect commands.
- Wait for explicit user approval for any cleanup or replacement action.

Why
- Distinguishes "host-key changed" from "missing key/config" quickly.
- Matches approval-gated workflow and avoids accidental key removal.
