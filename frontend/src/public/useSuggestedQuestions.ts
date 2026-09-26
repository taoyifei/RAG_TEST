import { useCallback, useEffect, useRef, useState } from "react";

import type {
  SuggestedQuestion,
  SuggestedQuestionStyle,
} from "./suggestedQuestions";
import type { PublicTurn } from "./usePublicChat";

const VISIBLE_COUNT = 5;
const EMPTY_CATALOG: readonly SuggestedQuestion[] = [];
const STYLE_QUOTAS: readonly [SuggestedQuestionStyle, number][] = [
  ["SHORT", 3],
  ["STANDARD", 1],
  ["COMPOUND", 1],
];

function questionKey(question: string): string {
  return question
    .normalize("NFKC")
    .replace(/\s+/gu, "")
    .replace(/[?？。！!]+$/gu, "")
    .toLocaleLowerCase();
}

function shuffle<T>(items: readonly T[], random: () => number): T[] {
  const result = [...items];
  for (let index = result.length - 1; index > 0; index -= 1) {
    const other = Math.floor(random() * (index + 1));
    [result[index], result[other]] = [result[other], result[index]];
  }
  return result;
}

function deduplicateQuestions(
  candidates: readonly SuggestedQuestion[],
): SuggestedQuestion[] {
  const ids = new Set<string>();
  const questions = new Set<string>();
  return candidates.filter((item) => {
    const normalizedQuestion = questionKey(item.question);
    if (ids.has(item.id) || questions.has(normalizedQuestion)) return false;
    ids.add(item.id);
    questions.add(normalizedQuestion);
    return true;
  });
}

function chooseDiverseQuestion(
  candidates: readonly SuggestedQuestion[],
  selected: readonly SuggestedQuestion[],
  random: () => number,
  style?: SuggestedQuestionStyle,
): SuggestedQuestion | undefined {
  const selectedIds = new Set(selected.map((item) => item.id));
  const selectedQuestions = new Set(
    selected.map((item) => questionKey(item.question)),
  );
  const selectedDocuments = new Set(selected.map((item) => item.documentId));
  const selectedTopics = new Set(selected.map((item) => item.topicKey));
  let best: SuggestedQuestion | undefined;
  let bestScore = -1;

  for (const item of shuffle(candidates, random)) {
    if (
      (style && item.style !== style) ||
      selectedIds.has(item.id) ||
      selectedQuestions.has(questionKey(item.question))
    ) {
      continue;
    }
    const score =
      (selectedDocuments.has(item.documentId) ? 0 : 2) +
      (selectedTopics.has(item.topicKey) ? 0 : 1);
    if (score > bestScore) {
      best = item;
      bestScore = score;
    }
  }
  return best;
}

function addQuestionsFromPool(
  candidates: readonly SuggestedQuestion[],
  selected: SuggestedQuestion[],
  random: () => number,
): void {
  for (const [style, quota] of STYLE_QUOTAS) {
    while (
      selected.length < VISIBLE_COUNT &&
      selected.filter((item) => item.style === style).length < quota
    ) {
      const item = chooseDiverseQuestion(candidates, selected, random, style);
      if (!item) break;
      selected.push(item);
    }
  }

  while (selected.length < VISIBLE_COUNT) {
    const item = chooseDiverseQuestion(candidates, selected, random);
    if (!item) break;
    selected.push(item);
  }
}

export function pickSuggestedQuestions(
  askedQuestions: readonly string[],
  previous: readonly SuggestedQuestion[],
  seenQuestionIds: ReadonlySet<string>,
  random: () => number = Math.random,
  catalog: readonly SuggestedQuestion[] = EMPTY_CATALOG,
): SuggestedQuestion[] {
  const asked = new Set(askedQuestions.map(questionKey));
  const previousIds = new Set(previous.map((item) => item.id));
  const previousQuestions = new Set(
    previous.map((item) => questionKey(item.question)),
  );
  const eligible = deduplicateQuestions(
    catalog
      .filter((item) => item.enabled)
      .filter((item) => !asked.has(questionKey(item.question))),
  );
  const available = eligible.filter(
    (item) =>
      !previousIds.has(item.id) &&
      !previousQuestions.has(questionKey(item.question)),
  );
  const fresh = available.filter((item) => !seenQuestionIds.has(item.id));
  const selected: SuggestedQuestion[] = [];

  addQuestionsFromPool(fresh, selected, random);
  if (selected.length < VISIBLE_COUNT) {
    addQuestionsFromPool(available, selected, random);
  }
  if (selected.length < VISIBLE_COUNT) {
    addQuestionsFromPool(eligible, selected, random);
  }
  return selected;
}

export function useSuggestedQuestions(
  turns: readonly PublicTurn[],
  identityKey?: string,
  catalog: readonly SuggestedQuestion[] = EMPTY_CATALOG,
  random: () => number = Math.random,
) {
  const [questions, setQuestions] = useState<readonly SuggestedQuestion[]>(() =>
    pickSuggestedQuestions([], [], new Set(), random, catalog),
  );
  const previousRef = useRef(questions);
  const seenQuestionIdsRef = useRef(new Set(questions.map((item) => item.id)));
  const askedQuestionsRef = useRef(new Set<string>());
  const rotatedTurnRef = useRef("");
  const identityKeyRef = useRef(identityKey);

  const reset = useCallback(() => {
    const next = pickSuggestedQuestions([], [], new Set(), random, catalog);
    previousRef.current = next;
    seenQuestionIdsRef.current = new Set(next.map((item) => item.id));
    askedQuestionsRef.current.clear();
    rotatedTurnRef.current = "";
    setQuestions(next);
  }, [catalog, random]);

  const rotate = useCallback(
    (askedQuestions: readonly string[]) => {
      const next = pickSuggestedQuestions(
        askedQuestions,
        previousRef.current,
        seenQuestionIdsRef.current,
        random,
        catalog,
      );
      previousRef.current = next;
      for (const item of next) seenQuestionIdsRef.current.add(item.id);
      setQuestions(next);
    },
    [catalog, random],
  );

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) reset();
    });
    return () => {
      active = false;
    };
  }, [reset]);

  useEffect(() => {
    if (!identityKey) {
      if (identityKeyRef.current) {
        identityKeyRef.current = undefined;
        reset();
      }
      return;
    }
    if (!identityKeyRef.current) {
      identityKeyRef.current = identityKey;
      return;
    }
    if (identityKeyRef.current === identityKey) return;
    identityKeyRef.current = identityKey;
    reset();
  }, [identityKey, reset]);

  useEffect(() => {
    for (const turn of turns) askedQuestionsRef.current.add(turn.question);
    const lastTurn = turns.at(-1);
    if (
      !lastTurn ||
      lastTurn.status === "submitting" ||
      lastTurn.status === "streaming"
    ) {
      return;
    }
    const turnKey = `${lastTurn.id}:${lastTurn.startedAt}`;
    if (rotatedTurnRef.current === turnKey) return;
    rotatedTurnRef.current = turnKey;
    rotate([...askedQuestionsRef.current]);
  }, [rotate, turns]);

  const refresh = useCallback(() => {
    rotate([...askedQuestionsRef.current]);
  }, [rotate]);

  return { questions, refresh };
}
