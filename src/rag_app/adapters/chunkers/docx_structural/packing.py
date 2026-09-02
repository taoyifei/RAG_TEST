"""同 run 顺序装包和尾块合并。"""

from __future__ import annotations

from rag_app.adapters.chunkers.docx_structural.atoms import AtomicUnit, RunPlan
from rag_app.adapters.chunkers.docx_structural.context import embedding_text
from rag_app.adapters.chunkers.docx_structural.rendering import render_atoms
from rag_app.adapters.chunkers.docx_structural.splitting import split_atom
from rag_app.core.models import ChunkingPolicy
from rag_app.core.ports import TokenCounterPort


def pack_run(
    run: RunPlan,
    *,
    document_title: str,
    policy: ChunkingPolicy,
    token_counter: TokenCounterPort,
) -> tuple[tuple[AtomicUnit, ...], ...]:
    """按目标距离和较早平局边界装包，不跨当前 run。

    Args:
        run: 一个禁止跨越的有序 run。
        document_title: embedding-only 文档标题。
        policy: provisional packing 参数。
        token_counter: 无网络 token 计数端口。

    Returns:
        每项均满足 citation/embedding hard max 的 atom pack。

    """
    atoms = tuple(
        segment
        for atom in run.atoms
        for segment in split_atom(
            atom,
            document_title=document_title,
            policy=policy,
            token_counter=token_counter,
        )
    )
    if not atoms:
        return ()
    packs: list[tuple[AtomicUnit, ...]] = []
    current: tuple[AtomicUnit, ...] = (atoms[0],)
    for atom in atoms[1:]:
        joined = (*current, atom)
        if _fits(joined, document_title, policy, token_counter):
            current_distance = abs(
                policy.target_tokens
                - _pack_size(current, document_title, token_counter)
            )
            joined_distance = abs(
                policy.target_tokens
                - _pack_size(joined, document_title, token_counter)
            )
            if joined_distance < current_distance:
                current = joined
                continue
        packs.append(current)
        current = (atom,)
    packs.append(current)
    if len(packs) > 1:
        tail = packs[-1]
        previous = packs[-2]
        if _pack_size(
            tail, document_title, token_counter
        ) < policy.min_tail_tokens and _fits(
            (*previous, *tail), document_title, policy, token_counter
        ):
            packs[-2:] = [(*previous, *tail)]
    return tuple(packs)


def _fits(
    atoms: tuple[AtomicUnit, ...],
    document_title: str,
    policy: ChunkingPolicy,
    token_counter: TokenCounterPort,
) -> bool:
    return _pack_size(atoms, document_title, token_counter) <= min(
        policy.hard_max_tokens, policy.effective_embedding_max
    )


def _pack_size(
    atoms: tuple[AtomicUnit, ...],
    document_title: str,
    token_counter: TokenCounterPort,
) -> int:
    rendered = render_atoms(atoms)
    citation_count = token_counter.count(rendered.text).count
    embedding_count = token_counter.count(
        embedding_text(document_title, atoms[0], rendered.text)
    ).count
    return max(citation_count, embedding_count)
