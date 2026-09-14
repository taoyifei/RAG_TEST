"""复用产品主库保存知识库回答与 OCR 选择，不改变文档向量配置。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, StrictInt, model_validator

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import NotFound
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.product.catalog import validate_model
from rag_app.product.control_store import ProductControlStore
from rag_app.product.ocr_adapters import LOCAL_OCR_CONNECTION_ID

_SHA256_LENGTH = 64
_LOCAL_OCR_MODEL = "pp-ocrv5-server"


class KnowledgeBaseModelSettings(FrozenModel):
    """可关闭的模型引用；授权由既有出站账本在发送边界执行。"""

    generation_connection_id: str | None = None
    generation_model: str | None = None
    generation_fallback_models: tuple[str, ...] = Field(
        default=(), max_length=4
    )
    rewrite_enabled: bool = False
    ocr_connection_id: str | None = None
    ocr_model: str | None = None
    ocr_enabled: bool = False
    ocr_media_hashes: tuple[str, ...] = Field(default=(), max_length=200)
    ocr_revision: StrictInt = Field(default=0, ge=0)
    budget_campaign_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$"
    )

    @model_validator(mode="after")
    def _paired_references(self) -> KnowledgeBaseModelSettings:
        if any(
            len(value) != _SHA256_LENGTH
            or any(char not in "0123456789abcdef" for char in value)
            for value in self.ocr_media_hashes
        ):
            raise ValueError("图片选择必须为 SHA256。")
        for connection, model in (
            (self.generation_connection_id, self.generation_model),
            (self.ocr_connection_id, self.ocr_model),
        ):
            if bool(connection) != bool(model):
                raise ValueError("模型与连接必须一起选择或清空。")
        if self.generation_fallback_models and not self.generation_model:
            raise ValueError("备用回答模型需要已选择的首选回答模型。")
        if len(set(self.generation_models)) != len(self.generation_models):
            raise ValueError("回答模型轮换链不允许重复模型。")
        if self.rewrite_enabled and not self.generation_connection_id:
            raise ValueError("改写需要已选择的回答模型。")
        if self.ocr_enabled and not self.ocr_connection_id:
            raise ValueError("图片识别需要已选择的 OCR 模型。")
        return self

    @property
    def generation_models(self) -> tuple[str, ...]:
        """返回按优先级排列且不包含空值的回答模型链。

        Args:
            无参数；读取当前冻结设置。

        Returns:
            首选模型与备用模型组成的有序元组。

        """
        if self.generation_model is None:
            return ()
        return (self.generation_model, *self.generation_fallback_models)


class ProductModelSettings:
    """主库内唯一的知识库生成/OCR 配置存储。"""

    def __init__(
        self, connections: SqliteConnectionFactory, control: ProductControlStore
    ) -> None:
        self.connections = connections
        self.control = control

    def get(self, knowledge_base_id: str) -> KnowledgeBaseModelSettings:
        """读取已存在知识库的配置，未配置返回明确关闭状态。

        Args:
            knowledge_base_id: 当前知识库标识。

        Returns:
            已保存配置或未配置默认值。

        """
        with self.connections.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM knowledge_bases WHERE knowledge_base_id = ? "
                    "AND deleted_at IS NULL",
                    (knowledge_base_id,),
                ).fetchone()
                is None
            ):
                raise NotFound("知识库不存在。", stage="models.settings")
            row = connection.execute(
                "SELECT configuration FROM knowledge_base_model_settings "
                "WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()
        return (
            KnowledgeBaseModelSettings()
            if row is None
            else KnowledgeBaseModelSettings.model_validate_json(row[0])
        )

    def save(
        self, knowledge_base_id: str, settings: KnowledgeBaseModelSettings
    ) -> KnowledgeBaseModelSettings:
        """验证已有连接和真实目录型号，只持久化不发送请求。

        Args:
            knowledge_base_id: 当前知识库标识。
            settings: 待验证的模型引用和开关。

        Returns:
            已保存的模型设置。

        """
        self.get(knowledge_base_id)
        generation_connection_id = settings.generation_connection_id
        if generation_connection_id:
            provider_connection = self.control.get_connection(
                generation_connection_id
            )
            if not provider_connection.enabled:
                raise ValueError("模型连接已停用。")
            operations = ["generation"]
            if settings.rewrite_enabled:
                operations.extend(("query.interpret", "query.rewrite"))
            for model in settings.generation_models:
                for operation in operations:
                    validate_model(
                        provider_connection.provider_type, model, operation
                    )
        if settings.ocr_connection_id and settings.ocr_model:
            if settings.ocr_connection_id == LOCAL_OCR_CONNECTION_ID:
                if settings.ocr_model != _LOCAL_OCR_MODEL:
                    raise ValueError("本地 OCR 只允许固定 PP-OCRv5 模型身份。")
            else:
                provider_connection = self.control.get_connection(
                    settings.ocr_connection_id
                )
                if not provider_connection.enabled:
                    raise ValueError("模型连接已停用。")
                validate_model(
                    provider_connection.provider_type,
                    settings.ocr_model,
                    "image.ocr",
                )
        with self.connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO knowledge_base_model_settings VALUES (?, ?, ?) "
                "ON CONFLICT(knowledge_base_id) DO UPDATE SET "
                "configuration=excluded.configuration, "
                "updated_at=excluded.updated_at",
                (
                    knowledge_base_id,
                    settings.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return settings

    def serving_identity(self, settings: KnowledgeBaseModelSettings) -> str:
        """模型、提示词、授权和凭据轮换只使查询缓存失效。

        Args:
            settings: 当前模型设置。

        Returns:
            不含密钥的查询缓存语义身份。

        """
        identity: dict[str, object] = {
            "settings": settings.model_dump(),
            "prompt": "grounded-chat-v7",
            "interpret": "bounded-interpret-v2",
            "rewrite": "bounded-rewrite-v3",
            "validation": "claim-support-v17",
            "answer_selection": "shared-query-semantics-v12",
            "generation_output": "grounded-output-4096-v1",
        }
        if settings.generation_connection_id:
            connection = self.control.get_connection(
                settings.generation_connection_id
            )
            identity["connection_version"] = connection.configuration_version
            identity["credential_version"] = self.control.credential_version(
                connection.credential_id
            )
        return canonical_sha256(identity)
