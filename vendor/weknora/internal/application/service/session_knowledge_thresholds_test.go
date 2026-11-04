package service

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/Tencent/WeKnora/internal/types"
)

func TestKnowledgeIDSnapshotUsesOriginalKBThresholdsOnlyForThisRequest(t *testing.T) {
	svc := &sessionService{
		knowledgeService: &keyKnowledgeLookup{rows: []*types.Knowledge{
			{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
		}},
		knowledgeBaseService: &keyKnowledgeBases{rows: []*types.KnowledgeBase{
			{ID: "kb-allowed", TenantID: 9},
		}},
	}
	ctx := keyFileServiceContext()
	ids := []string{"doc-1"}
	for _, useKBThresholds := range []bool{false, true, false, true} {
		targets, err := svc.buildSearchTargetsWithKBThresholds(ctx, 9, nil, ids, nil, useKBThresholds)
		if err != nil || len(targets) != 1 {
			t.Fatalf("build targets: targets=%v err=%v", targets, err)
		}
		target := targets[0]
		if target.Type != types.SearchTargetTypeKnowledge || !reflect.DeepEqual(target.KnowledgeIDs, ids) ||
			target.KnowledgeBaseID != "kb-allowed" || target.TenantID != 9 {
			t.Fatalf("threshold mode changed authorized scope: %#v", target)
		}
		if target.DisableRecallThresholds == useKBThresholds || targets.HasRecallThresholdOverride() == useKBThresholds {
			t.Fatalf("useKBThresholds=%v target=%#v", useKBThresholds, target)
		}
		// 下一个无标志的原生文件请求必须保留上游行为，不受上一轮快照影响。
		original, err := svc.buildSearchTargets(ctx, 9, nil, ids, nil)
		if err != nil || len(original) != 1 || !original[0].DisableRecallThresholds {
			t.Fatalf("default file behavior leaked from prior request: targets=%v err=%v", original, err)
		}
	}
	fullBefore, err := svc.buildSearchTargetsWithKBThresholds(ctx, 9, []string{"kb-allowed"}, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	fullAfter, err := svc.buildSearchTargetsWithKBThresholds(ctx, 9, []string{"kb-allowed"}, nil, nil, true)
	if err != nil || !reflect.DeepEqual(fullBefore, fullAfter) {
		t.Fatalf("whole KB search changed: before=%v after=%v err=%v", fullBefore, fullAfter, err)
	}
}

func TestKnowledgeThresholdModeSurvivesDurableResumeSnapshot(t *testing.T) {
	for _, useKBThresholds := range []bool{false, true} {
		request := &types.QARequest{
			KnowledgeIDs: []string{"doc-1"}, KnowledgeIDsUseKBThresholds: useKBThresholds,
		}
		saved := resumableAgentRequest(context.Background(), request)
		encoded, err := json.Marshal(saved)
		if err != nil {
			t.Fatal(err)
		}
		var decoded types.ResumableAgentRequest
		if err := json.Unmarshal(encoded, &decoded); err != nil {
			t.Fatal(err)
		}
		if decoded.KnowledgeIDsUseKBThresholds != useKBThresholds || !reflect.DeepEqual(decoded.KnowledgeIDs, request.KnowledgeIDs) {
			t.Fatalf("request threshold mode lost in resume: %s", encoded)
		}
	}
	var legacy types.ResumableAgentRequest
	if err := json.Unmarshal([]byte(`{"knowledge_ids":["doc-1"]}`), &legacy); err != nil || legacy.KnowledgeIDsUseKBThresholds {
		t.Fatalf("legacy snapshot must keep explicit-file behavior: request=%+v err=%v", legacy, err)
	}
}
