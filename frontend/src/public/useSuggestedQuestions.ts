import { useCallback, useEffect, useRef, useState } from "react";

import {
  SUGGESTED_QUESTIONS,
  type SuggestedQuestion,
} from "./suggestedQuestions";
import type { PublicTurn } from "./usePublicChat";

const VISIBLE_COUNT = 5;

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

function addDiverseQuestions(
  candidates: readonly SuggestedQuestion[],
  selected: SuggestedQuestion[],
  count: number,
  random: () => number,
): void {
  const shuffled = shuffle(candidates, random);
  const chosenIds = new Set(selected.map((item) => item.caseId));
  const chosenDocuments = new Set(selected.map((item) => item.documentId));
  for (const item of shuffled) {
    if (selected.length >= count) return;
    if (chosenIds.has(item.caseId) || chosenDocuments.has(item.documentId)) {
      continue;
    }
    selected.push(item);
    chosenIds.add(item.caseId);
    chosenDocuments.add(item.documentId);
  }
  for (const item of shuffled) {
    if (selected.length >= count) return;
    if (chosenIds.has(item.caseId)) continue;
    selected.push(item);
    chosenIds.add(item.caseId);
  }
}

export function pickSuggestedQuestions(
  askedQuestions: readonly string[],
  previous: readonly SuggestedQuestion[],
  seenIds: ReadonlySet<string>,
  random: () => number = Math.random,
): SuggestedQuestion[] {
  const asked = new Set(askedQuestions.map(questionKey));
  const previousIds = new Set(previous.map((item) => item.caseId));
  const available = SUGGESTED_QUESTIONS.filter(
    (item) =>
      !asked.has(questionKey(item.question)) && !previousIds.has(item.caseId),
  );
  const fresh = available.filter((item) => !seenIds.has(item.caseId));
  const selected: SuggestedQuestion[] = [];
  addDiverseQuestions(fresh, selected, VISIBLE_COUNT, random);
  if (selected.length < VISIBLE_COUNT) {
    addDiverseQuestions(available, selected, VISIBLE_COUNT, random);
  }
  return selected;
}

export function useSuggestedQuestions(turns: readonly PublicTurn[]) {
  const [questions, setQuestions] = useState<readonly SuggestedQuestion[]>(() =>
    pickSuggestedQuestions([], [], new Set()),
  );
  const previousRef = useRef(questions);
  const seenIdsRef = useRef(new Set(questions.map((item) => item.caseId)));
  const rotatedTurnRef = useRef("");

  const rotate = useCallback((askedQuestions: readonly string[]) => {
    const next = pickSuggestedQuestions(
      askedQuestions,
      previousRef.current,
      seenIdsRef.current,
    );
    previousRef.current = next;
    for (const item of next) seenIdsRef.current.add(item.caseId);
    setQuestions(next);
  }, []);

  useEffect(() => {
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
    rotate(turns.map((turn) => turn.question));
  }, [rotate, turns]);

  const refresh = useCallback(() => {
    rotate(turns.map((turn) => turn.question));
  }, [rotate, turns]);

  return { questions, refresh };
}
