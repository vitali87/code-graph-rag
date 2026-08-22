from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from ... import exceptions as ex
from ...constants import (
    ARCADE_COMMAND_PATH,
    ARCADE_HTTP_SCHEME,
    ARCADE_HTTP_TIMEOUT_S,
    ARCADE_KEY_COMMAND,
    ARCADE_KEY_LANGUAGE,
    ARCADE_KEY_RESULT,
    ARCADE_LANG_SQL,
)


class ArcadeHttpClient:
    """SQL over ArcadeDB's REST endpoint, used only for schema DDL.

    ArcadeDB's Bolt listener accepts Cypher and rejects SQL, but creating an
    index requires SQL (and requires the property to be declared first). This
    client covers exactly that gap; it is never on the ingestion hot path.
    """

    __slots__ = ("_base_url", "_auth_header")

    def __init__(
        self, host: str, port: int, database: str, username: str, password: str
    ) -> None:
        path = ARCADE_COMMAND_PATH.format(database=database)
        self._base_url = f"{ARCADE_HTTP_SCHEME}://{host}:{port}{path}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"

    def sql(self, command: str) -> list[dict[str, Any]]:
        payload = json.dumps(
            {ARCADE_KEY_LANGUAGE: ARCADE_LANG_SQL, ARCADE_KEY_COMMAND: command}
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed http scheme
            self._base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed http scheme
                request, timeout=ARCADE_HTTP_TIMEOUT_S
            ) as response:
                body = json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise ex.ArcadeHttpError(
                ex.ARCADE_HTTP_FAILED.format(
                    status=e.code, detail=e.reason, command=command
                )
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ex.ArcadeHttpError(
                ex.ARCADE_HTTP_FAILED.format(status="n/a", detail=e, command=command)
            ) from e
        result = body.get(ARCADE_KEY_RESULT, [])
        return list(result) if isinstance(result, list) else []
