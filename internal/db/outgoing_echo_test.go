package db

import "testing"

func TestReconcileOutgoingEcho_InferredMediaEchoCanPrecedePlaceholderTimestamp(t *testing.T) {
	store := newTestStore(t)
	const tmpID = "tmp_media_without_echo_id"

	if err := store.UpsertMessage(&Message{
		MessageID:      tmpID,
		ConversationID: "c1",
		TimestampMS:    21_000,
		Status:         "OUTGOING_SENDING",
		IsFromMe:       true,
		MediaID:        "local-upload-id",
		MimeType:       "image/png",
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertMessage(&Message{
		MessageID:      "real-media-id",
		ConversationID: "c1",
		TimestampMS:    1_000,
		Status:         "OUTGOING_DISPLAYED",
		IsFromMe:       true,
		MediaID:        "google-resized-id",
		MimeType:       "image/png",
	}); err != nil {
		t.Fatal(err)
	}

	result, err := store.ReconcileOutgoingEcho(&Message{
		MessageID:      "real-media-id",
		ConversationID: "c1",
		TimestampMS:    1_000,
		Status:         "OUTGOING_DISPLAYED",
		IsFromMe:       true,
		MediaID:        "google-resized-id",
		MimeType:       "image/png",
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.TmpID != tmpID || result.Strategy != OutgoingEchoMatchInferred {
		t.Fatalf("reconcile result = %+v", result)
	}
	if got, err := store.GetMessageByID(tmpID); err != nil {
		t.Fatal(err)
	} else if got != nil {
		t.Fatalf("media placeholder should be removed, got %+v", got)
	}
	if got, err := store.GetMessageByID("real-media-id"); err != nil {
		t.Fatal(err)
	} else if got == nil {
		t.Fatal("authoritative media echo should remain")
	}
}

func TestReconcileOutgoingEcho_DoesNotInferOutsideTimeWindow(t *testing.T) {
	store := newTestStore(t)
	if err := store.UpsertMessage(&Message{
		MessageID:      "tmp_too_old",
		ConversationID: "c1",
		Body:           "same body much later",
		TimestampMS:    1_000,
		Status:         "OUTGOING_SENDING",
		IsFromMe:       true,
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertMessage(&Message{
		MessageID:      "real-too-late",
		ConversationID: "c1",
		Body:           "same body much later",
		TimestampMS:    1_000 + outgoingEchoFallbackWindowMS + 1,
		Status:         "OUTGOING_DELIVERED",
		IsFromMe:       true,
	}); err != nil {
		t.Fatal(err)
	}

	result, err := store.ReconcileOutgoingEcho(&Message{
		MessageID:      "real-too-late",
		ConversationID: "c1",
		Body:           "same body much later",
		TimestampMS:    1_000 + outgoingEchoFallbackWindowMS + 1,
		Status:         "OUTGOING_DELIVERED",
		IsFromMe:       true,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.TmpID != "" {
		t.Fatalf("out-of-window placeholder was reconciled: %+v", result)
	}
	if got, err := store.GetMessageByID("tmp_too_old"); err != nil {
		t.Fatal(err)
	} else if got == nil {
		t.Fatal("out-of-window placeholder should be preserved")
	}
}
