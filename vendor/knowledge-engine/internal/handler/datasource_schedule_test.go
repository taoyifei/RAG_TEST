package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
	"industrialagent.local/knowledge-engine/internal/application/repository"
	"industrialagent.local/knowledge-engine/internal/application/service"
	"industrialagent.local/knowledge-engine/internal/datasource"
	"industrialagent.local/knowledge-engine/internal/types"
)

func TestDataSourceScheduleAPIRejectsInvalidCron(t *testing.T) {
	for _, operation := range []string{"create", "update", "resume"} {
		t.Run(operation, func(t *testing.T) {
			db, err := gorm.Open(sqlite.Open(filepath.Join(t.TempDir(), "schedule.db")), &gorm.Config{})
			require.NoError(t, err)
			require.NoError(t, db.AutoMigrate(&types.DataSource{}))
			repo := repository.NewDataSourceRepository(db)
			scheduler := datasource.NewScheduler(repo, nil, nil)
			kbSvc := &stubKBServiceForDS{getByID: func(context.Context, string) (*types.KnowledgeBase, error) {
				return &types.KnowledgeBase{ID: "kb-schedule", TenantID: 1}, nil
			}}
			svc := service.NewDataSourceService(repo, nil, nil, kbSvc, nil, nil, scheduler, nil, nil, nil)
			ds := &types.DataSource{
				ID: "ds-schedule", KnowledgeBaseID: "kb-schedule", TenantID: 1,
				Type: types.ConnectorTypeRSS, Status: types.DataSourceStatusActive,
				SyncSchedule: "0 0 * * * *",
			}
			if operation == "resume" {
				ds.Status = types.DataSourceStatusPaused
				ds.SyncSchedule = "invalid"
			}
			if operation != "create" {
				require.NoError(t, repo.Create(context.Background(), ds))
				require.NoError(t, scheduler.AddOrUpdate(ds))
			}
			entries := scheduler.EntryCount()
			h := NewDataSourceHandler(svc, kbSvc)
			router := newDataSourceTestRouter(h)
			router.POST("/datasource", h.CreateDataSource)
			router.PUT("/datasource/:id", h.UpdateDataSource)
			router.POST("/datasource/:id/resume", h.ResumeDataSource)
			body := *ds
			body.SyncSchedule = "invalid"
			payload, err := json.Marshal(body)
			require.NoError(t, err)
			method, url := http.MethodPost, "/datasource"
			switch operation {
			case "update":
				method, url = http.MethodPut, "/datasource/"+ds.ID
			case "resume":
				url = "/datasource/" + ds.ID + "/resume"
			}
			req := withDSCtx(httptest.NewRequest(method, url, bytes.NewReader(payload)), 1)
			req.Header.Set("Content-Type", "application/json")
			response := httptest.NewRecorder()
			router.ServeHTTP(response, req)
			require.Equal(t, http.StatusBadRequest, response.Code, response.Body.String())
			require.Contains(t, response.Body.String(), "invalid cron expression")
			require.Equal(t, entries, scheduler.EntryCount())
			saved, err := svc.GetDataSource(context.Background(), ds.ID)
			if operation == "create" {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
				require.Equal(t, ds.Status, saved.Status)
				require.Equal(t, ds.SyncSchedule, saved.SyncSchedule)
			}
		})
	}
}
