package sandbox

import "testing"

func TestIsStandardTemplateRecognizesProviderScopedName(t *testing.T) {
	for _, name := range []string{"knowledgeengine", "team/knowledgeengine", "project-b89e/KnowledgeEngine"} {
		if !isStandardTemplate(name) {
			t.Fatalf("expected %q to identify the KnowledgeEngine standard template", name)
		}
	}
	if isStandardTemplate("knowledgeengine-custom") {
		t.Fatal("custom template must not be treated as the standard template")
	}
	if isStandardTemplate(DesktopTemplateName) {
		t.Fatal("the desktop sibling must not be classified as the CLI standard template")
	}
}

func TestIsDesktopTemplateRecognizesProviderScopedName(t *testing.T) {
	for _, name := range []string{"knowledgeengine-desktop", "team/knowledgeengine-desktop", "project-b89e/KnowledgeEngine-Desktop"} {
		if !isDesktopTemplate(name) {
			t.Fatalf("expected %q to identify the KnowledgeEngine desktop template", name)
		}
	}
	if isDesktopTemplate("knowledgeengine") {
		t.Fatal("the CLI template must not be classified as desktop")
	}
	if isDesktopTemplate("knowledgeengine-desktop-custom") {
		t.Fatal("a similarly prefixed custom name must not be the desktop template")
	}
}

func TestClassifyKnowledgeEngineTemplatePrefersNameOverImage(t *testing.T) {
	standard, desktop := classifyKnowledgeEngineTemplate(DesktopTemplateName, DefaultDockerImage)
	if standard || !desktop {
		t.Fatalf(
			"named desktop template must be desktop even if the image repo matches CLI, got standard=%v desktop=%v",
			standard, desktop,
		)
	}
	standard, desktop = classifyKnowledgeEngineTemplate(StandardTemplateName, DefaultDesktopDockerImage)
	if !standard || desktop {
		t.Fatalf(
			"named CLI template must stay CLI even if the image tag is desktop, got standard=%v desktop=%v",
			standard, desktop,
		)
	}
}

func TestClassifyKnowledgeEngineTemplateNamelessImageUsesTag(t *testing.T) {
	standard, desktop := classifyKnowledgeEngineTemplate("", DefaultDockerImage)
	if !standard || desktop {
		t.Fatalf("CLI image with no name must be standard, got standard=%v desktop=%v", standard, desktop)
	}
	standard, desktop = classifyKnowledgeEngineTemplate("", DefaultDesktopDockerImage)
	if standard || !desktop {
		t.Fatalf("desktop image with no name must be desktop, got standard=%v desktop=%v", standard, desktop)
	}
	standard, desktop = classifyKnowledgeEngineTemplate("", DefaultCubeDesktopTemplateImage)
	if standard || !desktop {
		t.Fatalf("Cube desktop image with no name must be desktop, got standard=%v desktop=%v", standard, desktop)
	}
}
