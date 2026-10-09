# ChatGPT returned artifact custody

The sender and recurring collector download interpreter artifacts from the
exact terminal ChatGPT turn. During central admission, each local artifact is
rehash-checked, uploaded once to the private
`COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET`, and checked with an S3 SHA-256
readback. Its object key is content-addressed:

`chatmode-returned-artifacts/sha256/<first-two-hex>/<sha256>`

The Agent Memory conversation's `downloadable_files` entry contains the file
name, byte count, SHA-256, `storage_uri`, and `readback_verified: true`.
Downstream company workers should fetch that S3 URI with their company AWS
identity and verify SHA-256 before applying the artifact. A `sandbox:` link or
the local collector path is not a durable transfer reference.

Existing complete local collection records without central artifact custody
are upgraded by the recurring collector using the already-downloaded file.
This upgrade makes no ChatGPT.com request. If that file is unavailable, normal
provider readback can recover it only when the central observability policy
permits provider reads.
