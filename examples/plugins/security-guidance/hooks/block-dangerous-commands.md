---
name: block-dangerous-commands
enabled: true
event: bash
action: block
pattern: rm\s+-rf\s+[/~]|dd\s+if=|mkfs\.|format\s+[cde]:|>\s*/dev/sd
---

🛑 **Dangerous Command Blocked!**

This command can cause **irreversible data loss**:

- `rm -rf /` or `rm -rf ~` - Deletes everything
- `dd if=` - Can overwrite disks
- `mkfs.` - Formats filesystems
- `format` - Windows disk format
- Redirecting to `/dev/sd*` - Overwrites raw disk

## What to do instead

1. **Verify the exact path** you want to operate on
2. **Use safer alternatives**:
   - `rm -ri` (interactive mode)
   - `trash` command instead of rm
3. **Always have backups** before destructive operations

This operation has been **blocked** for your safety. If you really need to run this command, please do so manually outside of AnkaLoop.
