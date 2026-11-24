{{/*
Copyright 2025 Tencent
SPDX-License-Identifier: MIT

KnowledgeEngine Helm Chart Template Helpers

Best Practices References:
- https://helm.sh/docs/chart_best_practices/templates/
- https://github.com/argoproj/argo-helm/blob/main/charts/argo-cd/templates/_helpers.tpl
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "knowledgeengine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "knowledgeengine.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Resolve a component Service name while preserving names used by existing releases.
Usage: include "knowledgeengine.serviceName" (dict "component" "postgresql" "context" .)
*/}}
{{- define "knowledgeengine.serviceName" -}}
{{- $service := (index .context.Values .component).service | default dict -}}
{{- $legacyName := .component -}}
{{- if eq .component "postgresql" -}}
{{- $legacyName = "postgres" -}}
{{- end -}}
{{- default $legacyName $service.name -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
Ref: https://helm.sh/docs/chart_best_practices/labels/
*/}}
{{- define "knowledgeengine.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels following Kubernetes recommended labels.
Ref: https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/
*/}}
{{- define "knowledgeengine.labels" -}}
helm.sh/chart: {{ include "knowledgeengine.chart" . }}
{{ include "knowledgeengine.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: knowledgeengine
{{- end }}

{{/*
Selector labels
*/}}
{{- define "knowledgeengine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "knowledgeengine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component labels - use for individual components
Usage: {{ include "knowledgeengine.componentLabels" (dict "component" "app" "context" .) }}
*/}}
{{- define "knowledgeengine.componentLabels" -}}
{{ include "knowledgeengine.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels
Usage: {{ include "knowledgeengine.componentSelectorLabels" (dict "component" "app" "context" .) }}
*/}}
{{- define "knowledgeengine.componentSelectorLabels" -}}
{{ include "knowledgeengine.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Create the name of the service account to use.
Ref: https://helm.sh/docs/chart_best_practices/rbac/
*/}}
{{- define "knowledgeengine.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "knowledgeengine.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret name - supports existing secret
*/}}
{{- define "knowledgeengine.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "knowledgeengine.fullname" . }}-secrets
{{- end }}
{{- end }}

{{/*
Return the app image with tag.
Defaults to Chart.appVersion if tag is not specified.
*/}}
{{- define "knowledgeengine.app.image" -}}
{{- $tag := default .Chart.AppVersion .Values.app.image.tag }}
{{- printf "%s:%s" .Values.app.image.repository $tag }}
{{- end }}

{{/*
Return the frontend image with tag.
Defaults to Chart.appVersion if tag is not specified.
*/}}
{{- define "knowledgeengine.frontend.image" -}}
{{- $tag := default .Chart.AppVersion .Values.frontend.image.tag }}
{{- printf "%s:%s" .Values.frontend.image.repository $tag }}
{{- end }}

{{/*
Return the docreader image with tag.
Defaults to Chart.appVersion if tag is not specified.
*/}}
{{- define "knowledgeengine.docreader.image" -}}
{{- $tag := default .Chart.AppVersion .Values.docreader.image.tag }}
{{- printf "%s:%s" .Values.docreader.image.repository $tag }}
{{- end }}

{{/*
Return the PostgreSQL image with tag.
*/}}
{{- define "knowledgeengine.postgresql.image" -}}
{{- printf "%s:%s" .Values.postgresql.image.repository .Values.postgresql.image.tag }}
{{- end }}

{{/*
Return the Redis image with tag.
*/}}
{{- define "knowledgeengine.redis.image" -}}
{{- printf "%s:%s" .Values.redis.image.repository .Values.redis.image.tag }}
{{- end }}

{{/*
Return the Neo4j image with tag.
*/}}
{{- define "knowledgeengine.neo4j.image" -}}
{{- printf "%s:%s" .Values.neo4j.image.repository .Values.neo4j.image.tag }}
{{- end }}

{{/*
Create image pull secrets list.
*/}}
{{- define "knowledgeengine.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Return the storage class name.
*/}}
{{- define "knowledgeengine.storageClass" -}}
{{- if .Values.global.storageClass }}
{{- if eq .Values.global.storageClass "-" }}
storageClassName: ""
{{- else }}
storageClassName: {{ .Values.global.storageClass | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Pod security context.
Merges global defaults with component-specific overrides.
*/}}
{{- define "knowledgeengine.podSecurityContext" -}}
{{- $global := .Values.global.podSecurityContext | default dict }}
{{- $component := .componentSecurityContext | default dict }}
{{- $merged := merge $component $global }}
{{- if $merged }}
securityContext:
{{- toYaml $merged | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Container security context.
*/}}
{{- define "knowledgeengine.containerSecurityContext" -}}
{{- if . }}
securityContext:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
