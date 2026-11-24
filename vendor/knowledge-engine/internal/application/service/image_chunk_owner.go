package service

import (
	"industrialagent.local/knowledge-engine/internal/searchutil"
	"industrialagent.local/knowledge-engine/internal/types"
)

// A missing association is preferable to attaching evidence to an unrelated
// first paragraph. Exact Markdown targets also avoid URL-prefix collisions.
func imageChunkOwner(url string, chunks []types.ParsedChunk) string {
	if url == "" {
		return ""
	}
	owner := ""
	for _, c := range chunks {
		if c.ChunkID != "" && searchutil.ImageURLsInContent(c.Content)[url] {
			if owner != "" && owner != c.ChunkID {
				return ""
			}
			owner = c.ChunkID
		}
	}
	return owner
}
