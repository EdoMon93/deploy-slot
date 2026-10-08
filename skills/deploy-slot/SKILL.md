---
name: deploy-slot
description: Coordinate deployments to shared boxes over SSH. Use before deploying or preparing changes from a box's live configuration.
---

SSH_HOST is the SSH host alias used to connect to the deployment box.
Run deploy-slot on the destination with ordinary SSH. All SSH aliases and
users reaching that box share one reservation.

Reserve before preparing deployment changes from the box's live
configuration. Build and test code beforehand.

Commands:
- `ssh SSH_HOST deploy-slot reserve`: reserve for this session.
  Ownership comes from the forwarded Codex or Claude session ID.
  Fails if another session owns the box.
- `ssh SSH_HOST deploy-slot status`: show the owner and reservation time.
- `ssh SSH_HOST deploy-slot release`: release this session's reservation.
- `ssh SSH_HOST deploy-slot release --force`: clear an abandoned reservation.
  Use only after verifying the previous owner's deployment work has stopped.

Proceed only after reservation succeeds. Checking status does not reserve
the box. If another session owns it, continue independent work.
An SSH or identity-forwarding error does not mean the box is available.

Keep the reservation through deployment, verification and any rollback.
Release after that work finishes. If recovery remains unfinished,
keep the reservation and report the remaining work.
