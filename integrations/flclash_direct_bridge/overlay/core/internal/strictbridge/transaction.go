package strictbridge

import (
	"errors"
	"fmt"
	"sync"
)

var ErrRevision = errors.New("configuration revision changed")

type Engine[T any] struct {
	mu       sync.Mutex
	revision uint64
	current  T
}

func New[T any](current T) *Engine[T] { return &Engine[T]{current: current} }

func (e *Engine[T]) Snapshot() (T, uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.current, e.revision
}

func (e *Engine[T]) Change(expected uint64, parse func() (T, error), persist func(T) error, apply func(T) error) (err error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.revision != expected {
		return ErrRevision
	}
	candidate, err := parse()
	if err != nil {
		return fmt.Errorf("full parse refused: %w", err)
	}
	if err = persist(candidate); err != nil {
		return fmt.Errorf("persistence refused: %w", err)
	}
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("apply outcome unknown; recovery required: %v", value)
		}
	}()
	if err = apply(candidate); err != nil {
		return fmt.Errorf("apply refused; compensation required: %w", err)
	}
	e.current = candidate
	e.revision++
	return nil
}
