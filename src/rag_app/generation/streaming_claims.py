"""按当前 claims-only 协议增量识别完整 claim object。"""

from __future__ import annotations

import json

__all__ = ["IncrementalClaimsParser"]

_CLAIMS_KEY = '"claims"'
_JSON_WHITESPACE = frozenset(" \t\r\n")


class IncrementalClaimsParser:
    """从有界字符串流中只产出完整且可由标准 JSON 解析的 claim。"""

    def __init__(
        self,
        *,
        max_claims: int = 4,
        max_buffer_chars: int = 32_768,
    ) -> None:
        """冻结 claim 数量与累计字符上限。

        Args:
            max_claims: 允许识别的最大 claim 数量。
            max_buffer_chars: 单次回答允许累计的最大字符数。

        Raises:
            ValueError: 任一上限不是正数。

        """
        if max_claims <= 0 or max_buffer_chars <= 0:
            raise ValueError("增量 claim 解析上限必须为正数。")
        self._max_claims = max_claims
        self._max_buffer_chars = max_buffer_chars
        self._buffer = ""
        self._position = 0
        self._prefix_state = "root"
        self._array_started = False
        self._array_closed = False
        self._expect_claim = True
        self._allow_array_end = True
        self._claim_start: int | None = None
        self._claim_depth = 0
        self._in_string = False
        self._escaped = False
        self._claims: list[dict[str, object]] = []
        self._finished = False

    @property
    def claims(self) -> tuple[dict[str, object], ...]:
        """返回已完整闭合并通过 ``json.loads`` 的 claim。

        Args:
            无参数；读取当前解析状态。

        Returns:
            按模型输出顺序排列的独立 claim 映射。

        """
        return tuple(self._claims)

    def feed(self, fragment: str) -> tuple[dict[str, object], ...]:
        """追加任意字符串分片并返回本次新闭合的 claim。

        Args:
            fragment: 已由严格 UTF-8 解码器产出的任意字符串分片。

        Returns:
            本次分片使其完整闭合的零到多个 claim。

        Raises:
            TypeError: 分片不是字符串。
            ValueError: 前缀、数组、claim JSON 或累计上限无效。

        """
        if not isinstance(fragment, str):
            raise TypeError("claim stream fragment 必须是字符串。")
        if self._finished:
            raise ValueError("CLAIMS_STREAM_ALREADY_FINISHED")
        self._buffer += fragment
        if len(self._buffer) > self._max_buffer_chars:
            raise ValueError("CLAIMS_STREAM_TOO_LARGE")
        if not self._array_started and not self._consume_prefix():
            return ()
        if self._array_closed:
            return ()
        return self._consume_claims_array()

    def finish(self) -> None:
        """确认 claims 数组和顶层对象均已完整闭合。

        Args:
            无参数；校验当前累计缓冲区。

        Returns:
            无返回值。

        Raises:
            ValueError: 流未完成或顶层后缀不是唯一的右花括号。

        """
        if self._finished:
            return
        if (
            not self._array_started
            or not self._array_closed
            or self._claim_start is not None
        ):
            raise ValueError("INCOMPLETE_CLAIMS_STREAM")
        if self._buffer[self._position :].strip() != "}":
            raise ValueError("INVALID_CLAIMS_SUFFIX")
        self._finished = True

    def _consume_prefix(self) -> bool:
        """只接受当前协议唯一合法的顶层 ``claims`` 数组前缀。"""
        while True:
            self._skip_whitespace()
            if self._position >= len(self._buffer):
                return False
            if self._prefix_state == "root":
                self._consume_expected_character("{")
                self._prefix_state = "key"
                continue
            if self._prefix_state == "key":
                remaining = self._buffer[self._position :]
                if len(remaining) < len(_CLAIMS_KEY):
                    if not _CLAIMS_KEY.startswith(remaining):
                        raise ValueError("INVALID_CLAIMS_PREFIX")
                    return False
                if not remaining.startswith(_CLAIMS_KEY):
                    raise ValueError("INVALID_CLAIMS_PREFIX")
                self._position += len(_CLAIMS_KEY)
                self._prefix_state = "colon"
                continue
            if self._prefix_state == "colon":
                self._consume_expected_character(":")
                self._prefix_state = "array"
                continue
            self._consume_expected_character("[")
            self._array_started = True
            return True

    def _consume_claims_array(self) -> tuple[dict[str, object], ...]:
        emitted: list[dict[str, object]] = []
        while self._position < len(self._buffer):
            if self._claim_start is not None:
                completed = self._consume_claim_character()
                if completed is not None:
                    emitted.append(completed)
                continue
            self._skip_whitespace()
            if self._position >= len(self._buffer):
                break
            character = self._buffer[self._position]
            if self._expect_claim:
                if character == "]" and self._allow_array_end:
                    self._array_closed = True
                    self._position += 1
                    break
                if character != "{":
                    raise ValueError("INVALID_CLAIMS_ARRAY")
                self._claim_start = self._position
                self._claim_depth = 1
                self._in_string = False
                self._escaped = False
                self._position += 1
                continue
            if character == ",":
                self._expect_claim = True
                self._allow_array_end = False
                self._position += 1
                continue
            if character == "]":
                self._array_closed = True
                self._position += 1
                break
            raise ValueError("INVALID_CLAIMS_ARRAY")
        return tuple(emitted)

    def _consume_claim_character(self) -> dict[str, object] | None:
        character = self._buffer[self._position]
        self._position += 1
        if self._in_string:
            if self._escaped:
                self._escaped = False
            elif character == "\\":
                self._escaped = True
            elif character == '"':
                self._in_string = False
            return None
        if character == '"':
            self._in_string = True
            return None
        if character == "{":
            self._claim_depth += 1
            return None
        if character != "}":
            return None
        self._claim_depth -= 1
        if self._claim_depth > 0:
            return None
        claim_start = self._claim_start
        if claim_start is None:
            raise ValueError("INVALID_STREAMED_CLAIM_STATE")
        raw_claim = self._buffer[claim_start : self._position]
        self._claim_start = None
        self._expect_claim = False
        self._allow_array_end = True
        try:
            parsed = json.loads(raw_claim)
        except json.JSONDecodeError as error:
            raise ValueError("INVALID_STREAMED_CLAIM_JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("INVALID_STREAMED_CLAIM_JSON")
        if len(self._claims) >= self._max_claims:
            raise ValueError("TOO_MANY_STREAMED_CLAIMS")
        claim = {str(key): value for key, value in parsed.items()}
        self._claims.append(claim)
        return claim

    def _skip_whitespace(self) -> None:
        while (
            self._position < len(self._buffer)
            and self._buffer[self._position] in _JSON_WHITESPACE
        ):
            self._position += 1

    def _consume_expected_character(self, expected: str) -> None:
        if self._buffer[self._position] != expected:
            raise ValueError("INVALID_CLAIMS_PREFIX")
        self._position += 1
