package com.qtcinfotech.jarvis.wake

import android.app.Activity
import android.util.Log
import com.google.firebase.FirebaseException
import com.google.firebase.auth.FirebaseAuth
import com.google.firebase.auth.FirebaseAuthInvalidCredentialsException
import com.google.firebase.auth.PhoneAuthCredential
import com.google.firebase.auth.PhoneAuthOptions
import com.google.firebase.auth.PhoneAuthProvider
import expo.modules.kotlin.Promise
import java.util.concurrent.TimeUnit

/**
 * Sign in by phone number (Nov 2026): Firebase sends the SMS code and checks it on this phone; what the app gets back
 * is Firebase's signed proof (an ID token) that this phone holds that number, which the JARVIS server verifies and
 * turns into its own login. Firebase keeps no account state here: the proof is taken and Firebase is signed out.
 *
 * On most phones the code is read from the SMS by itself (verification completes without typing); otherwise the
 * person types it. Firebase checks that the app is genuine (Play Integrity); where that is not possible it opens a
 * reCAPTCHA page once.
 */
object PhoneOtp {
    private const val TAG = "JarvisOtp"
    private var verificationId: String? = null
    private var resendToken: PhoneAuthProvider.ForceResendingToken? = null
    private var pending: Promise? = null
    private var proofPromise: Promise? = null

    private val auth: FirebaseAuth get() = FirebaseAuth.getInstance()

    /** Sends the code. Resolves {"status": "sent"} when the SMS is on its way, or {"status": "verified", "id_token": …}
     *  when the phone read the code by itself. Rejects "otp_failed" with a plain reason. */
    fun start(activity: Activity?, phone: String, promise: Promise) {
        if (activity == null) {
            promise.reject("otp_failed", "The app is not in front.", null)
            return
        }
        pending?.reject("otp_cancelled", "A new code was requested.", null)
        pending = promise
        val callbacks = object : PhoneAuthProvider.OnVerificationStateChangedCallbacks() {
            override fun onVerificationCompleted(credential: PhoneAuthCredential) {
                Log.i(TAG, "code read by the phone itself")
                val p = pending ?: proofPromise
                pending = null
                proofPromise = null
                signIn(credential, p, auto = true)
            }

            override fun onVerificationFailed(e: FirebaseException) {
                Log.w(TAG, "verification failed: ${e.message}")
                val p = pending
                pending = null
                val why = when {
                    e is FirebaseAuthInvalidCredentialsException -> "That does not look like a valid phone number."
                    (e.message ?: "").contains("quota", true) -> "Too many codes were sent to this number today. Try later."
                    (e.message ?: "").contains("network", true) -> "No internet. Check the connection and try again."
                    else -> e.message ?: "The code could not be sent."
                }
                p?.reject("otp_failed", why, e)
            }

            override fun onCodeSent(id: String, token: PhoneAuthProvider.ForceResendingToken) {
                Log.i(TAG, "code sent")
                verificationId = id
                resendToken = token
                val p = pending
                pending = null
                p?.resolve(mapOf("status" to "sent"))
            }
        }
        try {
            auth.setLanguageCode("en")
            val builder = PhoneAuthOptions.newBuilder(auth)
                .setPhoneNumber(phone)
                .setTimeout(60L, TimeUnit.SECONDS)
                .setActivity(activity)
                .setCallbacks(callbacks)
            resendToken?.let { builder.setForceResendingToken(it) }
            PhoneAuthProvider.verifyPhoneNumber(builder.build())
        } catch (e: Throwable) {
            pending = null
            promise.reject("otp_failed", e.message ?: "The code could not be sent.", e)
        }
    }

    /** The code the person typed. Resolves {"status": "verified", "id_token": …}. */
    fun confirm(code: String, promise: Promise) {
        val id = verificationId
        if (id == null) {
            promise.reject("otp_failed", "Ask for a code first.", null)
            return
        }
        val credential = try {
            PhoneAuthProvider.getCredential(id, code.trim())
        } catch (e: Throwable) {
            promise.reject("otp_failed", "Enter the 6-digit code from the SMS.", e)
            return
        }
        proofPromise = promise
        signIn(credential, promise, auto = false)
    }

    /** A code arrived by itself before the person typed: the app waits on this to get the proof. */
    fun awaitAuto(promise: Promise) {
        proofPromise = promise
    }

    fun cancel() {
        pending?.reject("otp_cancelled", "Cancelled.", null)
        pending = null
        proofPromise = null
        verificationId = null
    }

    private fun signIn(credential: PhoneAuthCredential, promise: Promise?, auto: Boolean) {
        auth.signInWithCredential(credential)
            .addOnSuccessListener { result ->
                val user = result.user
                if (user == null) {
                    promise?.reject("otp_failed", "Firebase gave no user.", null)
                    return@addOnSuccessListener
                }
                user.getIdToken(true)
                    .addOnSuccessListener { t ->
                        val token = t.token ?: ""
                        auth.signOut()  // the proof is all JARVIS needs; nothing stays signed in to Firebase
                        verificationId = null
                        resendToken = null
                        if (token.isEmpty()) promise?.reject("otp_failed", "Firebase gave no proof.", null)
                        else promise?.resolve(mapOf("status" to "verified", "id_token" to token, "auto" to auto))
                    }
                    .addOnFailureListener { e -> promise?.reject("otp_failed", e.message ?: "Could not get the proof.", e) }
            }
            .addOnFailureListener { e ->
                val why = if (e is FirebaseAuthInvalidCredentialsException) "That code is wrong. Check the SMS and try again."
                else e.message ?: "The code could not be checked."
                promise?.reject("otp_wrong", why, e)
            }
    }
}
