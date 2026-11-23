# keys

`example_key.pub` is a throwaway public key for the example profiles; its
private half was discarded. For real hosts create your own key and point
`RESCUE_PUBKEY` or `ACME_PUSH_PUBKEY` at it.

```bash
ssh-keygen -t ed25519 -N '' -C deploy-tool -f ~/.ssh/deploy
```

Only public keys belong in this directory. The `.gitignore` excludes
everything except this file and `example_key.pub`.
