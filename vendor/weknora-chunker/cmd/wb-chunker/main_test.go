package main

import (
	"reflect"
	"testing"

	"github.com/Tencent/WeKnora/internal/infrastructure/chunker"
)

func TestParentChildMatchesUpstream(t *testing.T) {
	text := "# 标题\n\n" + "第一段包含中文和 English words。\n\n" + "第二段继续说明。\n\n"
	req := request{Text: text, Mode: "parent_child", Strategy: "auto", ChunkSize: 32, Overlap: 8, ParentSize: 56, ChildSize: 24}
	got, err := run(req)
	if err != nil {
		t.Fatal(err)
	}
	base := chunker.NormalizeSplitterConfig(chunker.SplitterConfig{ChunkSize: req.ChunkSize, ChunkOverlap: req.Overlap, Strategy: req.Strategy})
	parentCfg, childCfg := chunker.DeriveParentChildConfigs(base, req.ParentSize, req.ChildSize)
	want, diag := chunker.SplitParentChildWithDiagnostics(text, parentCfg, childCfg)
	if got.Diagnostics.SelectedTier != diag.SelectedTier || len(got.Parents) != len(want.Parents) || len(got.Children) != len(want.Children) {
		t.Fatalf("wrapper diverges from upstream: got %+v want %+v", got.Diagnostics, diag)
	}
	for i, item := range want.Parents {
		if !reflect.DeepEqual(got.Parents[i], fromChunk(item, -1)) {
			t.Fatalf("parent %d diverges", i)
		}
	}
	for i, item := range want.Children {
		if !reflect.DeepEqual(got.Children[i], fromChunk(item.Chunk, item.ParentIndex)) {
			t.Fatalf("child %d diverges", i)
		}
	}
}

func TestSingleRuneOffsets(t *testing.T) {
	got, err := run(request{Text: "甲乙\n丙丁", Mode: "single", Strategy: "auto", ChunkSize: 2})
	if err != nil {
		t.Fatal(err)
	}
	runes := []rune("甲乙\n丙丁")
	for _, item := range got.Children {
		if string(runes[item.Start:item.End]) != item.Content {
			t.Fatalf("rune positions do not reconstruct %+v", item)
		}
	}
}
