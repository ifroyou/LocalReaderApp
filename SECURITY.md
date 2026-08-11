# Security policy

## Default security model

The default launcher binds the reader service to `127.0.0.1` and sets `LOCAL_READER_CLOUD_ENABLED=0`. Documents and generated audio are expected to stay on the local computer. Do not expose the service to a public network without a separate threat-model review.

Never commit API keys, cloud service-role keys, access tokens, private documents, runtime logs, or generated caches. A service-role key must never be shipped to end users; the public local-first build does not include a cloud credential template.

## Reporting a vulnerability

Please do not publish credentials or exploit details in a public issue. Open a private GitHub security report for the repository, or contact the maintainer through the GitHub profile, with:

- the affected commit or release;
- reproducible steps and expected impact;
- whether a document, credential, or network service is exposed; and
- a safe way to contact you for follow-up.

Please allow time for a fix and coordinated disclosure before public discussion.
