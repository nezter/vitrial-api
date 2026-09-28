# Device Pairing and Session Security

This flow replaces normal copy/paste bearer-token onboarding with a single-use, short-lived pairing grant while preserving the existing server-authoritative Connected Operations profile.

## Operator flow

1. Provision or update the user's organization membership through the existing internal admin bootstrap boundary.
2. Create a pairing grant for that membership with `POST /internal/admin/v1/pairing-grants` and the separately managed `X-Vitrial-Admin-Key`.
3. Give the returned `pairingCode` to the intended device through an authenticated operational channel. The code is returned once, is not stored in plaintext by the API, expires by default after 15 minutes, and can be consumed only once.
4. The iOS app exchanges the code with `POST /api/v1/auth/pair`. The API mints a normal hashed bearer session, consumes the grant transactionally, and returns the bearer once with `Cache-Control: no-store`.
5. The iOS app immediately calls `/api/v1/auth/me`. The device saves the bearer only after this existing Connected Operations authority check succeeds.
6. `POST /api/v1/auth/logout` revokes the current bearer session server-side before the iOS app removes the local credential.

Example grant request (placeholder values only):

```bash
curl --fail-with-body \
  -H "X-Vitrial-Admin-Key: $VITRIAL_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"membershipID":"MEMBERSHIP_ID","grantTTLSeconds":900,"sessionTTLSeconds":28800}' \
  https://api.example.com/internal/admin/v1/pairing-grants
```

Do not place the admin key, returned pairing code, or bearer token in source control, logs, issue comments, screenshots, shell history intended for collection, or long-lived configuration. The admin API remains excluded from the public OpenAPI surface.

## Resource bounds

The same hardening slice pins structured sync to the iOS client's existing 200-record batching contract, limits V2 native JSON record payloads to 1.5 MB, bounds V1 wire payloads and identifiers, and enforces a configurable evidence upload ceiling while streaming. The evidence default is 100 MiB (`EVIDENCE_MAX_BYTES=104857600`); production may reduce it to the largest evidence object the field workflow actually needs.

These application limits are defense in depth. A public deployment should also enforce ingress request-size and rate limits at the trusted edge, especially for the unauthenticated pairing exchange, and should retain the existing HTTPS-only remote transport policy.
