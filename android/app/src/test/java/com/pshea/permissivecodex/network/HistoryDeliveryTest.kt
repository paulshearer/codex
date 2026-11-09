package com.pshea.permissivecodex.network

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class HistoryDeliveryTest {
    @Test fun lostReplyRetriesTheSameAcceptedRequest() = runBlocking {
        val accepted = mutableSetOf<String>()
        val submitted = mutableListOf<String>()
        val submissionId = "same-submission"
        val result = deliverHistoryRequest {
            submitted += submissionId
            accepted += submissionId
            if (submitted.size == 1) throw IOException("stream was reset: CANCEL")
            "replacement-chat"
        }
        assertEquals("replacement-chat", result)
        assertEquals(2, submitted.size)
        assertEquals(1, accepted.size)
    }

    @Test fun validationErrorsDoNotRetry() = runBlocking {
        var attempts = 0
        try {
            deliverHistoryRequest<String> { attempts++; throw CompanionException(409, "conflict", "Finish the active response") }
            fail("Expected conflict")
        } catch (error: CompanionException) { assertEquals(409, error.status) }
        assertEquals(1, attempts)
    }

    @Test fun deliberateCancellationDoesNotResubmit() = runBlocking {
        val started = CompletableDeferred<Unit>()
        var attempts = 0
        val job = launch {
            deliverHistoryRequest<String> {
                attempts++; started.complete(Unit)
                try { awaitCancellation() } finally { throw IOException("stream was reset: CANCEL") }
            }
        }
        started.await(); job.cancelAndJoin()
        assertEquals(1, attempts)
    }
}
