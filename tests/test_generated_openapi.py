from app.main import app


def test_generated_openapi_keeps_v1_paths_and_operation_ids():
    spec = app.openapi()
    expected = {
        "/health": {"get": "health"},
        "/api/v1/version": {"get": "version"},
        "/api/v1/auth/me": {"get": "authMe"},
        "/api/v1/auth/pair": {"post": "authPair"},
        "/api/v1/auth/logout": {"post": "authLogout"},
        "/api/v1/sync/push": {"post": "syncPush"},
        "/api/v1/sync/pull": {"get": "syncPull"},
        "/api/v1/sync/evidence-blobs/{documentID}": {
            "head": "evidencePresence",
            "put": "evidenceUpload",
            "get": "evidenceDownload",
        },
    }

    for path, operations in expected.items():
        assert path in spec["paths"]
        for method, operation_id in operations.items():
            assert spec["paths"][path][method]["operationId"] == operation_id

    assert "/internal/admin/v1/pairing-grants" not in spec["paths"]
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    upload = spec["paths"]["/api/v1/sync/evidence-blobs/{documentID}"]["put"]
    assert "application/octet-stream" in upload["requestBody"]["content"]
