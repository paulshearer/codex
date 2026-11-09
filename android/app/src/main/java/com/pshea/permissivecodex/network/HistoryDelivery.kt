package com.pshea.permissivecodex.network

import java.io.IOException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive

/** History requests carry a durable submission UUID; retries reuse that request. */
internal suspend fun <T> deliverHistoryRequest(request: suspend () -> T): T {
    for (attempt in 0..2) {
        currentCoroutineContext().ensureActive()
        try { return request() }
        catch (error: IOException) {
            currentCoroutineContext().ensureActive()
            if (error is CompanionException || attempt == 2) throw error
            delay(if (attempt == 0) 200 else 600)
        }
    }
    error("History delivery did not complete")
}
