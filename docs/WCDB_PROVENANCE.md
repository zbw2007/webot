# WCDB Runtime Provenance

The repository currently contains no approved `native/windows/wcdb_api.dll`.
Do not place an unreviewed DLL in that path, load it, or connect this runtime to
the primary WeChat account.

Before approval, record all of the following from an auditable source or
reproducible local build:

- Source URL:
- Download date:
- Release tag or commit:
- SHA-256:
- Authenticode signature status:
- Signer:
- Windows Defender result:
- Verifier:
- Compatible WeChat version:

Until these fields are reviewed and a hash is supplied through the local
`WCDB_ALLOWED_SHA256` environment variable, class-assistant startup must fail.
