// wb-chunker 以有界 JSON 进程封装固定版本的 WeKnora 分块算法。
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"unicode/utf8"

	"github.com/Tencent/WeKnora/internal/infrastructure/chunker"
)

const maxRequestBytes = 8 << 20
const upstreamCommit = "1edcd54b43606d9079bb36650efe3f68707a79ea"

type request struct {
	Text         string   `json:"normalized_text"`
	Mode         string   `json:"mode"`
	Strategy     string   `json:"strategy"`
	ChunkSize    int      `json:"chunk_size"`
	Overlap      int      `json:"overlap"`
	ParentSize   int      `json:"parent_size"`
	ChildSize    int      `json:"child_size"`
	TokenLimit   int      `json:"token_limit"`
	Languages    []string `json:"language_hints"`
}

type wireChunk struct {
	Content       string `json:"content"`
	ContextHeader string `json:"context_header"`
	Seq           int    `json:"seq"`
	Start         int    `json:"start"`
	End           int    `json:"end"`
	ParentIndex   int    `json:"parent_index"`
}

type response struct {
	UpstreamCommit string               `json:"upstream_commit"`
	OffsetUnit     string               `json:"offset_unit"`
	Parents        []wireChunk          `json:"parents"`
	Children       []wireChunk          `json:"children"`
	Diagnostics    *chunker.Diagnostics `json:"diagnostics"`
}

func main() {
	input, err := io.ReadAll(io.LimitReader(os.Stdin, maxRequestBytes+1))
	if err != nil || len(input) > maxRequestBytes {
		fail("request exceeds limit or cannot be read")
	}
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.DisallowUnknownFields()
	var req request
	if err := decoder.Decode(&req); err != nil {
		fail("invalid JSON request")
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		fail("multiple JSON values are forbidden")
	}
	out, err := run(req)
	if err != nil {
		fail(err.Error())
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fail("cannot encode response")
	}
}

func fail(message string) {
	fmt.Fprintln(os.Stderr, message)
	os.Exit(2)
}

func run(req request) (response, error) {
	out := response{
		UpstreamCommit: upstreamCommit,
		OffsetUnit:     "unicode_codepoint",
		Parents:        []wireChunk{},
		Children:       []wireChunk{},
	}
	if !utf8.ValidString(req.Text) {
		return out, errors.New("normalized_text is not valid UTF-8")
	}
	if req.Strategy == "" {
		req.Strategy = chunker.StrategyAuto
	}
	switch req.Strategy {
	case chunker.StrategyAuto, chunker.StrategyHeading,
		chunker.StrategyHeuristic, chunker.StrategyRecursive,
		chunker.StrategyLegacy:
	default:
		return out, errors.New("unknown strategy")
	}
	if req.ChunkSize < 0 || req.Overlap < 0 || req.ParentSize < 0 || req.ChildSize < 0 || req.TokenLimit < 0 {
		return out, errors.New("negative size is forbidden")
	}
	config := chunker.NormalizeSplitterConfig(chunker.SplitterConfig{
		ChunkSize:    req.ChunkSize,
		ChunkOverlap: req.Overlap,
		Strategy:     req.Strategy,
		TokenLimit:   req.TokenLimit,
		Languages:    req.Languages,
	})
	if req.Mode == "single" {
		chunks, diag := chunker.SplitWithDiagnostics(req.Text, config)
		out.Diagnostics = diag
		for _, item := range chunks {
			out.Children = append(out.Children, fromChunk(item, -1))
		}
		return out, nil
	}
	if req.Mode != "parent_child" {
		return out, errors.New("mode must be single or parent_child")
	}
	parentConfig, childConfig := chunker.DeriveParentChildConfigs(config, req.ParentSize, req.ChildSize)
	result, diag := chunker.SplitParentChildWithDiagnostics(req.Text, parentConfig, childConfig)
	out.Diagnostics = diag
	for _, item := range result.Parents {
		out.Parents = append(out.Parents, fromChunk(item, -1))
	}
	for _, item := range result.Children {
		out.Children = append(out.Children, fromChunk(item.Chunk, item.ParentIndex))
	}
	return out, nil
}

func fromChunk(item chunker.Chunk, parentIndex int) wireChunk {
	return wireChunk{
		Content:       item.Content,
		ContextHeader: item.ContextHeader,
		Seq:           item.Seq,
		Start:         item.Start,
		End:           item.End,
		ParentIndex:   parentIndex,
	}
}
