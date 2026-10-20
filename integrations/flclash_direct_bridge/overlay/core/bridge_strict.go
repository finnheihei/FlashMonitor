package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/metacubex/mihomo/config"
	"github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/hub"
	"github.com/metacubex/mihomo/hub/executor"
)

const strictBridgeParserAudited = false

type bridgeStrictPrepareParams struct {
	Candidate string `json:"candidate"`
}

type bridgeStrictApplyParams struct {
	Ticket string `json:"ticket"`
}

type bridgeStrictTicket struct {
	config        *config.Config
	base          *config.Config
	candidateHash string
	expires       time.Time
}

var bridgeStrictTickets = map[string]bridgeStrictTicket{}

func bridgeHash(data []byte) string {
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

func bridgeNonRules(data []byte) (string, error) {
	raw, err := config.UnmarshalRawConfig(data)
	if err != nil {
		return "", err
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return "", err
	}
	var values map[string]json.RawMessage
	if err = json.Unmarshal(encoded, &values); err != nil {
		return "", err
	}
	delete(values, "rules")
	delete(values, "Rules")
	encoded, err = json.Marshal(values)
	return bridgeHash(encoded), err
}

func bridgeStrictPrepare(params *bridgeStrictPrepareParams) (map[string]string, error) {
	if !strictBridgeParserAudited {
		return nil, fmt.Errorf("pinned_parser_side_effect_audit_required")
	}
	if len(params.Candidate) == 0 || len(params.Candidate) > 8*1024*1024 {
		return nil, fmt.Errorf("invalid_candidate_size")
	}
	configMu.Lock()
	defer configMu.Unlock()
	old, err := os.ReadFile(filepath.Join(constant.Path.HomeDir(), "config.yaml"))
	if err != nil {
		return nil, err
	}
	before, err := bridgeNonRules(old)
	if err != nil {
		return nil, err
	}
	after, err := bridgeNonRules([]byte(params.Candidate))
	if err != nil || before != after {
		return nil, fmt.Errorf("non_rule_settings_changed")
	}
	candidate, err := executor.ParseWithBytes([]byte(params.Candidate))
	if err != nil {
		return nil, err
	}
	for key, ticket := range bridgeStrictTickets {
		if time.Now().After(ticket.expires) {
			delete(bridgeStrictTickets, key)
		}
	}
	if len(bridgeStrictTickets) >= 8 {
		return nil, fmt.Errorf("ticket_budget_exceeded")
	}
	hash := bridgeHash([]byte(params.Candidate))
	nonce := make([]byte, 32)
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	ticket := hex.EncodeToString(nonce)
	bridgeStrictTickets[ticket] = bridgeStrictTicket{candidate, currentConfig, hash, time.Now().Add(30 * time.Second)}
	return map[string]string{"ticket": ticket, "candidate_hash": hash, "nonrules_hash": before}, nil
}

func bridgeStrictApply(params *bridgeStrictApplyParams) (err error) {
	if !strictBridgeParserAudited {
		return fmt.Errorf("pinned_parser_side_effect_audit_required")
	}
	configMu.Lock()
	defer configMu.Unlock()
	ticket, exists := bridgeStrictTickets[params.Ticket]
	if !exists || time.Now().After(ticket.expires) {
		return fmt.Errorf("ticket_expired")
	}
	delete(bridgeStrictTickets, params.Ticket)
	if currentConfig != ticket.base {
		return fmt.Errorf("runtime_changed")
	}
	generated, err := os.ReadFile(filepath.Join(constant.Path.HomeDir(), "config.yaml"))
	if err != nil || bridgeHash(generated) != ticket.candidateHash {
		return fmt.Errorf("generated_candidate_changed")
	}
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("apply_outcome_unknown_recovery_required: %v", value)
		}
	}()
	hub.ApplyConfig(ticket.config)
	updateListeners(ticket.config)
	currentConfig = ticket.config
	reconcileGeoUpdater()
	return nil
}

func init() {
	registerMethod(bridgeStrictCapabilitiesMethod, withoutArguments(func(response MethodResponse) {
		response.success(map[string]any{"protocol": 1, "parser_side_effects_audited": strictBridgeParserAudited, "strict_load_preserves_old": strictBridgeParserAudited})
	}))
	registerMethod(bridgeStrictPrepareMethod, withArguments(func(params *bridgeStrictPrepareParams, response MethodResponse) {
		result, err := bridgeStrictPrepare(params)
		if err != nil {
			response.failure("strict_refused", err.Error(), nil)
			return
		}
		response.success(result)
	}))
	registerMethod(bridgeStrictApplyMethod, withArguments(func(params *bridgeStrictApplyParams, response MethodResponse) {
		if err := bridgeStrictApply(params); err != nil {
			response.failure("strict_refused", err.Error(), nil)
			return
		}
		response.success(true)
	}))
}
