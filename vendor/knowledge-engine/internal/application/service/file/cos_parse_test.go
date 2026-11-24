package file

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseCosObjectName_RejectsLocalScheme(t *testing.T) {
	svc := &cosFileService{bucketURL: "https://b.cos.ap-shanghai.myqcloud.com/"}
	_, err := svc.parseCosObjectName("local://10000/exports/img.png")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "local")
}

func TestParseCosObjectName_CosScheme(t *testing.T) {
	svc := &cosFileService{bucketURL: "https://b.cos.ap-shanghai.myqcloud.com/"}
	key, err := svc.parseCosObjectName("cos://bucket/ap-shanghai/knowledgeengine/10000/exports/a.png")
	require.NoError(t, err)
	assert.Equal(t, "knowledgeengine/10000/exports/a.png", key)
}

func TestParseCosObjectName_RejectsMinioScheme(t *testing.T) {
	svc := &cosFileService{bucketURL: "https://b.cos.ap-shanghai.myqcloud.com/"}
	_, err := svc.parseCosObjectName("minio://wizard-test/10000/exports/img.png")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "minio")
}

// The tenant check scans every path segment, so a numeric bucket or region
// segment could pass as the caller's tenant (42 here) while the object key
// reads tenant 7's file from the shared bucket. Both must match the service.
func TestParseCosObjectName_RejectsForeignBucketOrRegion(t *testing.T) {
	svc := &cosFileService{
		bucketURL:  "https://knowledgeengine-1250000000.cos.ap-shanghai.myqcloud.com/",
		bucketName: "knowledgeengine-1250000000",
		region:     "ap-shanghai",
	}
	for _, path := range []string{
		"cos://42/ap-shanghai/knowledgeengine/7/knowledge/secret.pdf",
		"cos://knowledgeengine-1250000000/42/knowledgeengine/7/knowledge/secret.pdf",
	} {
		_, err := svc.parseCosObjectName(path)
		require.Error(t, err, path)
	}
	key, err := svc.parseCosObjectName("cos://knowledgeengine-1250000000/ap-shanghai/knowledgeengine/42/exports/a.png")
	require.NoError(t, err)
	assert.Equal(t, "knowledgeengine/42/exports/a.png", key)
}
