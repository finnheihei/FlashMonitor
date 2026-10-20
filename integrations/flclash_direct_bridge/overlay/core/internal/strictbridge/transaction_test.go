package strictbridge

import (
	"errors"
	"strings"
	"testing"
)

func TestBadSemanticRuleDoesNotPersistApplyOrFallback(t *testing.T) {
	e := New("old-listeners-and-rules")
	called := false
	err := e.Change(0, func() (string, error) { return "", errors.New("NOT-A-RULE") },
		func(string) error { called = true; return nil }, func(string) error { called = true; return nil })
	current, revision := e.Snapshot()
	if err == nil || called || current != "old-listeners-and-rules" || revision != 0 {
		t.Fatal("semantic error changed old state", err, current, revision)
	}
}

func TestSuccessAndStaleNativeEdit(t *testing.T) {
	e := New("old")
	var sequence []string
	err := e.Change(0, func() (string, error) { sequence = append(sequence, "parse"); return "new", nil },
		func(string) error { sequence = append(sequence, "persist"); return nil },
		func(string) error { sequence = append(sequence, "apply"); return nil })
	if err != nil || strings.Join(sequence, ",") != "parse,persist,apply" {
		t.Fatal(err, sequence)
	}
	err = e.Change(0, func() (string, error) { t.Fatal("stale parse called"); return "", nil }, nil, nil)
	if !errors.Is(err, ErrRevision) {
		t.Fatal(err)
	}
}

func TestPersistFailurePreservesCurrent(t *testing.T) {
	e := New("old")
	err := e.Change(0, func() (string, error) { return "new", nil },
		func(string) error { return errors.New("disk full") }, func(string) error { t.Fatal("apply called"); return nil })
	current, revision := e.Snapshot()
	if err == nil || current != "old" || revision != 0 {
		t.Fatal(err, current, revision)
	}
}

func TestApplyPanicIsUnknownAndDoesNotPublishSuccess(t *testing.T) {
	e := New("old")
	err := e.Change(0, func() (string, error) { return "new", nil }, func(string) error { return nil },
		func(string) error { panic("listener failed") })
	current, revision := e.Snapshot()
	if err == nil || !strings.Contains(err.Error(), "unknown") || current != "old" || revision != 0 {
		t.Fatal(err, current, revision)
	}
}
