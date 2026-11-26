package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.RadialGradient
import android.graphics.Shader
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import kotlin.math.abs
import kotlin.math.sin

/**
 * The floating JARVIS button (his ask, 26 Nov 2026): a small orb that sits over whatever app is open, like a chat
 * head. A tap opens JARVIS on the conversation, listening (WakeService.onBubbleTap); a drag moves it, and it
 * settles on the nearest edge, where he last left it. It is hidden while the JARVIS app itself is in front (the app
 * has its own orb) and while a voice-only conversation shows its strip.
 *
 * Needs "Display over other apps". Android never draws it over the lock screen: there the notification's
 * "Talk to JARVIS" button and the wake word do the same job.
 */
class FloatingBubble(private val context: Context, private val onTap: () -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val prefs = WakeBus.prefs(context)
    private var view: OrbView? = null
    private var params: WindowManager.LayoutParams? = null
    private var shown = false

    private fun dp(v: Float): Int = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, context.resources.displayMetrics).toInt()

    fun canShow(): Boolean = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)

    fun show() {
        main.post {
            if (shown || !canShow()) return@post
            val v = view ?: OrbView(context).also { view = it }
            val p = params ?: params().also { params = it }
            try {
                wm.addView(v, p)
                shown = true
                v.start()
            } catch (_: Throwable) {
            }
        }
    }

    fun hide() {
        main.post {
            if (!shown) return@post
            shown = false
            try {
                view?.stop()
                wm.removeView(view)
            } catch (_: Throwable) {
            }
        }
    }

    private fun params(): WindowManager.LayoutParams {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        val size = dp(SIZE_DP)
        val metrics = context.resources.displayMetrics
        return WindowManager.LayoutParams(
            size, size, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            // Where he last left it; the first time, the right edge a little below the middle.
            x = prefs.getInt(WakeBus.KEY_BUBBLE_X, metrics.widthPixels - size - dp(6f))
            y = prefs.getInt(WakeBus.KEY_BUBBLE_Y, (metrics.heightPixels * 0.58f).toInt())
        }
    }

    /** Drag and tap on the orb: a small move is a tap, a longer one carries it and lets it settle on an edge. */
    private inner class OrbView(context: Context) : View(context) {
        private val d = resources.displayMetrics.density
        private val began = SystemClock.uptimeMillis()
        private var running = false
        private val slop = ViewConfiguration.get(context).scaledTouchSlop
        private var downX = 0f
        private var downY = 0f
        private var startX = 0
        private var startY = 0
        private var dragging = false
        private val halo = Paint(Paint.ANTI_ALIAS_FLAG)
        private val core = Paint(Paint.ANTI_ALIAS_FLAG)
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
        private var heart: Shader? = null
        private var glow: Shader? = null

        fun start() {
            running = true
            invalidate()
        }

        fun stop() {
            running = false
        }

        override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
            val r = w / 2f
            heart = RadialGradient(r * 0.8f, r * 0.7f, r * 0.9f, intArrayOf(Color.rgb(170, 236, 255), Color.rgb(96, 110, 232), Color.rgb(44, 32, 140)),
                floatArrayOf(0f, 0.55f, 1f), Shader.TileMode.CLAMP)
            glow = RadialGradient(r, r, r, intArrayOf(Color.argb(120, 120, 200, 255), Color.argb(0, 120, 200, 255)), null, Shader.TileMode.CLAMP)
        }

        override fun onDraw(canvas: Canvas) {
            val w = width.toFloat()
            val cx = w / 2f
            val t = (SystemClock.uptimeMillis() - began) / 1000f
            val pulse = 1f + 0.04f * sin(t * 1.6f)
            halo.shader = glow
            canvas.save()
            canvas.translate(cx, cx)
            canvas.scale(pulse, pulse)
            canvas.drawCircle(0f, 0f, cx, halo)
            canvas.restore()
            core.shader = heart
            canvas.drawCircle(cx, cx, cx * 0.68f, core)
            // Two fine rings turning the other way from each other: the reactor of the app's own orb, in small.
            ring.color = Color.argb(200, 160, 230, 255)
            ring.strokeWidth = 1.2f * d
            val r1 = cx * 0.8f
            for (i in 0 until 3) canvas.drawArc(cx - r1, cx - r1, cx + r1, cx + r1, t * 60f + i * 120f, 70f, false, ring)
            ring.color = Color.argb(170, 190, 160, 255)
            ring.strokeWidth = 1.6f * d
            val r2 = cx * 0.56f
            for (i in 0 until 2) canvas.drawArc(cx - r2, cx - r2, cx + r2, cx + r2, -t * 90f + i * 180f, 110f, false, ring)
            core.shader = null
            core.color = Color.WHITE
            canvas.drawCircle(cx, cx, cx * 0.16f, core)
            if (running) postInvalidateOnAnimation()
        }

        override fun onTouchEvent(event: MotionEvent): Boolean {
            val p = params ?: return false
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startX = p.x
                    startY = p.y
                    dragging = false
                    return true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - downX
                    val dy = event.rawY - downY
                    if (!dragging && (abs(dx) > slop || abs(dy) > slop)) dragging = true
                    if (dragging) {
                        p.x = (startX + dx).toInt()
                        p.y = (startY + dy).toInt()
                        try {
                            wm.updateViewLayout(this, p)
                        } catch (_: Throwable) {
                        }
                    }
                    return true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    if (!dragging) {
                        if (event.actionMasked == MotionEvent.ACTION_UP) {
                            performClick()
                            onTap()
                        }
                        return true
                    }
                    settle(p)
                    return true
                }
            }
            return false
        }

        override fun performClick(): Boolean {
            super.performClick()
            return true
        }

        /** Let go: the orb slides to the nearest side edge and stays inside the screen; the place is remembered. */
        private fun settle(p: WindowManager.LayoutParams) {
            val metrics = resources.displayMetrics
            val size = width
            val edgeX = if (p.x + size / 2 < metrics.widthPixels / 2) dp(6f) else metrics.widthPixels - size - dp(6f)
            val top = dp(40f)
            val bottom = metrics.heightPixels - size - dp(40f)
            p.x = edgeX
            p.y = p.y.coerceIn(top, bottom)
            try {
                wm.updateViewLayout(this, p)
            } catch (_: Throwable) {
            }
            prefs.edit().putInt(WakeBus.KEY_BUBBLE_X, p.x).putInt(WakeBus.KEY_BUBBLE_Y, p.y).apply()
        }
    }

    companion object {
        private const val SIZE_DP = 58f
    }
}
