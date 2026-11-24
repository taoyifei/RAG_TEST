package service

import (
	"time"

	"github.com/hibiken/asynq"
	"industrialagent.local/knowledge-engine/internal/config"
	"industrialagent.local/knowledge-engine/internal/types"
)

func documentProcessTaskOptions(cfg *config.Config, extra ...asynq.Option) []asynq.Option {
	opts := []asynq.Option{
		asynq.Queue(types.QueueDefault),
		asynq.Timeout(config.DocumentProcessTimeout(cfg)),
		asynq.MaxRetry(3),
	}
	opts = append(opts, extra...)
	return opts
}

func knowledgePostProcessTaskOptions() []asynq.Option {
	return []asynq.Option{
		asynq.Queue(types.QueuePostProcess),
		asynq.MaxRetry(3),
		asynq.Timeout(30 * time.Minute),
	}
}
