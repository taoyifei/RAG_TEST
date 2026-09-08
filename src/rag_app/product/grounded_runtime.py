"""将已有模型连接与知识库资料范围绑定到生成和一次改写。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager

from rag_app.adapters.providers.aliyun_chat import AliyunChatConfig, ChatMessage
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.adapters.providers.budget_transport import (
    provider_budget_scope,
    provider_data_scope,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.application.retrieval.rewrite_constraints import (
    rewrite_constraint_reason,
)
from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.errors import PolicyDenied, RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerDraft,
    ProviderCall,
    ProviderHealth,
    QueryVariant,
    SearchRequest,
)
from rag_app.core.ports import GenerationRequest
from rag_app.core.ports.query_rewrite import RewriteOutcome
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.provider_runtime import ProviderRuntimeRegistry

_REWRITE_SIGNAL = re.compile(
    r"这个|那个|它|其中|上述|前者|后者|具体干什么|干啥|干什么|咋|怎么说|说白了|那怎么办"
)
_MAX_REWRITE_CHARS = 512


class ProductGroundedModel:
    """一个知识库的模型引用，每次发送都重新核对来源与持久授权。"""

    def __init__(
        self,
        settings: KnowledgeBaseModelSettings,
        knowledge_base_id: str,
        connections: SqliteConnectionFactory,
        providers: ProviderRuntimeRegistry,
    ) -> None:
        self.settings = settings
        self.knowledge_base_id = knowledge_base_id
        self.connections = connections
        self.providers = providers
        with connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM knowledge_bases "
                "WHERE knowledge_base_id = ? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            raise PolicyDenied("知识库不可用。", stage="generation.scope")
        self.project_id = str(row[0])
        if (
            not settings.generation_connection_id
            or not settings.generation_model
        ):
            raise ValueError("回答模型尚未配置。")
        self.adapter = providers.chat_adapter(
            settings.generation_connection_id,
            model=settings.generation_model,
            config=AliyunChatConfig(
                model=settings.generation_model, egress_allowed=True
            ),
        )

    @property
    def descriptor(self) -> ComponentDescriptor:
        """保留真实 Provider 组件身份。

        Args:
            无参数；读取当前配置。

        Returns:
            真实模型适配器描述符。

        """
        return self.adapter.descriptor

    @property
    def capabilities(self) -> ComponentCapabilities:
        """保留真实生成能力。

        Args:
            无参数；读取当前适配器。

        Returns:
            当前适配器声明的能力。

        """
        return self.adapter.capabilities

    def health(self, *, network: bool = False) -> ProviderHealth:
        """健康读取默认不出网。

        Args:
            network: 是否明确允许实际探测。

        Returns:
            不含凭据的健康状态。

        """
        return self.adapter.health(network=network)

    @contextmanager
    def _scope(
        self, operation: str, source_hashes: tuple[str, ...] = ()
    ) -> Iterator[None]:
        campaign_id = self.settings.budget_campaign_id
        if campaign_id is None:
            raise PolicyDenied(
                "当前知识库尚未绑定资料出网授权。",
                stage="generation.authorization",
                code="DATA_EGRESS_NOT_AUTHORIZED",
            )
        ledger = ProviderBudgetLedger(
            self.connections.database_path.parent / "provider-budget.sqlite3"
        )
        campaign = ledger.campaign(campaign_id)
        with (
            provider_budget_scope(
                ledger,
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                scope=campaign.scope,
                step_id=operation,
            ),
            provider_data_scope(
                project_id=self.project_id,
                knowledge_base_id=self.knowledge_base_id,
                source_hashes=source_hashes,
            ),
        ):
            yield

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """只授权本次有限证据所对应的现存文档，不读取整份正文。

        Args:
            request: 原问题和有界证据包。

        Returns:
            尚待应用层验证的生成草稿。

        """
        hashes: set[str] = set()
        with self.connections.transaction() as connection:
            for item in request.evidence:
                row = connection.execute(
                    "SELECT v.content_sha256 FROM document_versions v "
                    "JOIN documents d ON d.document_id=v.document_id "
                    "WHERE v.document_version_id=? AND d.document_id=? "
                    "AND d.project_id=? AND d.knowledge_base_id=? "
                    "AND d.deleted_at IS NULL AND d.status='active'",
                    (
                        item.document_version_id,
                        item.document_id,
                        self.project_id,
                        self.knowledge_base_id,
                    ),
                ).fetchone()
                if row is None:
                    raise PolicyDenied(
                        "生成来源已不可用。",
                        stage="generation.scope",
                        code="GENERATION_SOURCE_UNAVAILABLE",
                    )
                hashes.add(str(row[0]))
        with self._scope("generation", tuple(sorted(hashes))):
            return self.adapter.generate(request)

    def rewrite(  # noqa: PLR0911
        self, request: SearchRequest, *, recall_insufficient: bool = False
    ) -> RewriteOutcome:
        """仅口语/指代或首轮不足时调用一次，非法变更直接丢弃。

        Args:
            request: 原问题和已鉴权会话范围。
            recall_insufficient: 是否由首轮召回不足触发。

        Returns:
            可选补充检索变体和实际调用审计。

        """
        if not self.settings.rewrite_enabled or (
            not recall_insufficient and not _REWRITE_SIGNAL.search(request.text)
        ):
            return RewriteOutcome()
        if len(request.text) > _MAX_REWRITE_CHARS:
            return RewriteOutcome(
                reason_code="REWRITE_INPUT_LIMIT", attempted=True
            )
        messages = (
            ChatMessage(
                role="system",
                content=(
                    '将用户问题改写为适合资料检索的一个独立问题。只输出JSON对象{"query":"问题"}。'
                    "原对象、编号、日期、数字、否定和范围必须保留，不添加事实、不回答问题。"
                    "保留原问题的业务词项和限制词，仅调整问句语法或词序；不要用别的对象或业务同义词替换。"
                    "上下文仅用于消歧，不能当事实证据。不能确定时原样返回。"
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "question": request.text,
                        "context": [
                            value[:300]
                            for value in request.conversation_context[-2:]
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        call: ProviderCall | None = None
        try:
            with self._scope("query.rewrite"):
                result = self.adapter.complete(
                    messages, operation="query.rewrite", max_output_tokens=256
                )
            call = result.call
            payload = json.loads(result.content)
            text = payload.get("query") if isinstance(payload, dict) else None
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= _MAX_REWRITE_CHARS
            ):
                return RewriteOutcome(
                    calls=(result.call,),
                    reason_code="REWRITE_INVALID",
                    attempted=True,
                )
            constraint_reason = rewrite_constraint_reason(request, text)
            if constraint_reason is not None:
                return RewriteOutcome(
                    calls=(result.call,),
                    reason_code=constraint_reason,
                    attempted=True,
                )
            variant = (
                None
                if text == request.text
                else QueryVariant(
                    text=text,
                    kind="rewrite",
                    identity=canonical_sha256(
                        {"query": text, "policy": "bounded-rewrite-v3"}
                    ),
                )
            )
            return RewriteOutcome(
                variant,
                (result.call,),
                "REWRITE_APPLIED" if variant else "REWRITE_UNCHANGED",
                True,
            )
        except RagError as error:
            calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            return RewriteOutcome(
                calls=calls, reason_code=error.code, attempted=True
            )
        except (ValueError, KeyError):
            return RewriteOutcome(
                calls=() if call is None else (call,),
                reason_code="REWRITE_INVALID",
                attempted=True,
            )

    def close(self) -> None:
        """释放当前 adapter 的 HTTP 连接池。

        Args:
            无参数；关闭当前适配器。

        Returns:
            关闭成功时无返回值。

        """
        self.adapter.close()
