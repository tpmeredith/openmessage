package db

import (
	"database/sql"
	"errors"
	"strings"
	"time"
)

const outgoingEchoFallbackWindowMS int64 = 30_000

const (
	OutgoingEchoMatchExplicit = "explicit_tmp_id"
	OutgoingEchoMatchInferred = "unique_content_time"
)

// OutgoingEchoReconcileResult describes the optimistic placeholder replaced by
// an authoritative outgoing transport echo. A zero value means that no safe
// one-to-one match was available.
type OutgoingEchoReconcileResult struct {
	TmpID    string
	Strategy string
}

// ReconcileOutgoingEcho removes the local tmp_ placeholder corresponding to an
// authoritative outgoing message and repoints its send key to the real message
// ID. Google normally returns the request tmp ID on the echo, but some SMS/RCS
// echoes omit it. In that case, reconcile only when content, conversation,
// reply/media shape, and timestamp identify exactly one pending placeholder.
func (s *Store) ReconcileOutgoingEcho(echo *Message, explicitTmpID string) (OutgoingEchoReconcileResult, error) {
	if echo == nil || !echo.IsFromMe || strings.TrimSpace(echo.MessageID) == "" ||
		strings.HasPrefix(echo.MessageID, "tmp_") || strings.TrimSpace(echo.ConversationID) == "" {
		return OutgoingEchoReconcileResult{}, nil
	}

	tx, err := s.db.Begin()
	if err != nil {
		return OutgoingEchoReconcileResult{}, err
	}
	defer tx.Rollback()

	// The caller stores the authoritative echo first. Requiring that row keeps
	// this helper from repointing a send key to a message that is not durable.
	var echoExists int
	if err := tx.QueryRow(`
		SELECT COUNT(*)
		FROM messages
		WHERE message_id = ? AND conversation_id = ? AND is_from_me = 1
	`, echo.MessageID, echo.ConversationID).Scan(&echoExists); err != nil {
		return OutgoingEchoReconcileResult{}, err
	}
	if echoExists != 1 {
		return OutgoingEchoReconcileResult{}, nil
	}

	result := OutgoingEchoReconcileResult{}
	explicitTmpID = strings.TrimSpace(explicitTmpID)
	if explicitTmpID != "" && explicitTmpID != echo.MessageID {
		var candidate string
		err := tx.QueryRow(`
			SELECT message_id
			FROM messages
			WHERE message_id = ?
			  AND conversation_id = ?
			  AND is_from_me = 1
			  AND message_id GLOB 'tmp_*'
		`, explicitTmpID, echo.ConversationID).Scan(&candidate)
		switch {
		case err == nil:
			result = OutgoingEchoReconcileResult{TmpID: candidate, Strategy: OutgoingEchoMatchExplicit}
		case errors.Is(err, sql.ErrNoRows):
			// Fall through to the conservative content/time match. Some Google
			// echoes carry a tmp ID that is absent from the local store.
		default:
			return OutgoingEchoReconcileResult{}, err
		}
	}

	if result.TmpID == "" && echo.TimestampMS > 0 {
		candidates, err := inferredOutgoingEchoCandidates(tx, echo)
		if err != nil {
			return OutgoingEchoReconcileResult{}, err
		}
		if len(candidates) == 1 {
			result = OutgoingEchoReconcileResult{TmpID: candidates[0], Strategy: OutgoingEchoMatchInferred}
		}
	}
	if result.TmpID == "" {
		return OutgoingEchoReconcileResult{}, nil
	}

	// An echo is authoritative evidence that an ambiguous/failed dispatch did
	// send. Update any matching key as well as the normal key==tmp-ID case.
	if _, err := tx.Exec(`
		UPDATE outgoing_send_keys
		SET message_id = ?, status = ?, updated_at = ?
		WHERE conversation_id = ?
		  AND (idempotency_key = ? OR message_id = ?)
	`, echo.MessageID, OutgoingSendStatusSent, time.Now().UnixMilli(), echo.ConversationID, result.TmpID, result.TmpID); err != nil {
		return OutgoingEchoReconcileResult{}, err
	}

	deleted, err := s.deleteMessages(tx, `
		message_id = ?
		AND conversation_id = ?
		AND is_from_me = 1
		AND message_id GLOB 'tmp_*'
	`, result.TmpID, echo.ConversationID)
	if err != nil {
		return OutgoingEchoReconcileResult{}, err
	}
	rows, err := deleted.RowsAffected()
	if err != nil {
		return OutgoingEchoReconcileResult{}, err
	}
	if rows != 1 {
		return OutgoingEchoReconcileResult{}, nil
	}
	if err := tx.Commit(); err != nil {
		return OutgoingEchoReconcileResult{}, err
	}
	return result, nil
}

func inferredOutgoingEchoCandidates(tx *sql.Tx, echo *Message) ([]string, error) {
	mediaID := strings.TrimSpace(echo.MediaID)
	mimeType := strings.TrimSpace(echo.MimeType)
	rows, err := tx.Query(`
		SELECT message_id
		FROM messages
		WHERE conversation_id = ?
		  AND message_id GLOB 'tmp_*'
		  AND is_from_me = 1
		  AND status = 'OUTGOING_SENDING'
		  AND body IS ?
		  AND reply_to_id IS ?
		  AND timestamp_ms BETWEEN ? AND ?
		  AND (
			(? = '' AND COALESCE(media_id, '') = '')
			OR
			(? <> '' AND COALESCE(media_id, '') <> ''
			 AND (? = '' OR COALESCE(mime_type, '') = ?))
		  )
		ORDER BY ABS(timestamp_ms - ?), message_id
		LIMIT 2
	`, echo.ConversationID, echo.Body, echo.ReplyToID,
		echo.TimestampMS-outgoingEchoFallbackWindowMS,
		echo.TimestampMS+outgoingEchoFallbackWindowMS,
		mediaID, mediaID, mimeType, mimeType, echo.TimestampMS)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var candidates []string
	for rows.Next() {
		var candidate string
		if err := rows.Scan(&candidate); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	return candidates, rows.Err()
}
