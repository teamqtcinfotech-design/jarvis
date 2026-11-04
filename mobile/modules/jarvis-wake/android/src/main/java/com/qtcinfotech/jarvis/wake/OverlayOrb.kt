package com.qtcinfotech.jarvis.wake

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.animation.LinearInterpolator
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The floating JARVIS orb that appears over whatever is on screen while a voice-only conversation runs
 * (like an assistant sheet): a pulsing ball, the current line, and a Stop button. Needs "Display over other
 * apps". It cannot appear over the lock screen (Android forbids it); there the voice alone carries on.
 */
class OverlayOrb(private val context: Context, private val onStop: () -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var root: LinearLayout? = null
    private var orb: OrbView? = null
    private var line: TextView? = null
    private var shown = false

    private fun dp(v: Float): Int = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, context.resources.displayMetrics).toInt()

    fun canShow(): Boolean = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)

    fun show(text: String, state: String) {
        main.post {
            if (!canShow()) return@post
            if (root == null) build()
            line?.text = text
            orb?.state = state
            if (!shown) {
                try {
                    wm.addView(root, params())
                    shown = true
                    orb?.start()
                } catch (_: Throwable) {
                }
            }
        }
    }

    fun update(text: String?, state: String?) {
        main.post {
            if (!shown) return@post
            if (text != null) line?.text = text
            if (state != null) orb?.state = state
        }
    }

    fun hide() {
        main.post {
            if (!shown) return@post
            try {
                orb?.stop()
                wm.removeView(root)
            } catch (_: Throwable) {
            }
            shown = false
        }
    }

    private fun params(): WindowManager.LayoutParams {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        return WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            y = dp(24f)
        }
    }

    private fun build() {
        val card = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(20f), dp(18f), dp(20f), dp(16f))
            background = GradientDrawable().apply {
                cornerRadius = dp(28f).toFloat()
                setColor(Color.argb(245, 255, 255, 255))
                setStroke(dp(1f), Color.argb(255, 230, 230, 234))
            }
            elevation = dp(10f).toFloat()
        }
        val margin = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16f), 0, dp(16f), 0)
            addView(card, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        val orbView = OrbView(context)
        card.addView(orbView, LinearLayout.LayoutParams(dp(84f), dp(84f)))
        val title = TextView(context).apply {
            text = "JARVIS"
            setTextColor(Color.rgb(13, 13, 13))
            textSize = 13f
            letterSpacing = 0.15f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(10f), 0, dp(2f))
        }
        card.addView(title)
        val lineView = TextView(context).apply {
            setTextColor(Color.rgb(26, 26, 26))
            textSize = 16f
            gravity = Gravity.CENTER
            maxLines = 4
            setPadding(dp(8f), dp(2f), dp(8f), dp(8f))
        }
        card.addView(lineView)
        val stop = TextView(context).apply {
            text = "Stop"
            setTextColor(Color.rgb(107, 107, 118))
            textSize = 14f
            setPadding(dp(18f), dp(8f), dp(18f), dp(8f))
            background = GradientDrawable().apply {
                cornerRadius = dp(999f).toFloat()
                setColor(Color.rgb(244, 244, 245))
            }
            setOnClickListener { onStop() }
        }
        card.addView(stop)
        root = margin
        orb = orbView
        line = lineView
    }

    /** A dark ball with a soft ring that breathes (idle), pulses gold (listening), spins grey (thinking), ripples (speaking). */
    private class OrbView(context: Context) : View(context) {
        var state: String = "idle"
            set(value) {
                field = value
                invalidate()
            }
        private var phase = 0f
        private val animator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 1300
            repeatCount = ValueAnimator.INFINITE
            interpolator = LinearInterpolator()
            addUpdateListener {
                phase = it.animatedValue as Float
                invalidate()
            }
        }
        private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(13, 13, 13) }
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }

        fun start() = animator.start()
        fun stop() = animator.cancel()

        override fun onDraw(canvas: Canvas) {
            val cx = width / 2f
            val cy = height / 2f
            val r = width * 0.30f
            val accent = when (state) {
                "listening" -> Color.rgb(184, 134, 79)
                "speaking" -> Color.rgb(184, 134, 79)
                "thinking" -> Color.rgb(161, 161, 170)
                else -> Color.rgb(230, 230, 234)
            }
            val grow = when (state) {
                "listening" -> 1f + 0.55f * phase
                "speaking" -> 1f + 0.25f * kotlin.math.abs(0.5f - phase) * 2f
                "thinking" -> 1.2f
                else -> 1f + 0.08f * kotlin.math.abs(0.5f - phase) * 2f
            }
            val alpha = when (state) {
                "listening" -> ((1f - phase) * 160).toInt()
                "thinking" -> 140
                else -> 90
            }
            ring.color = accent
            ring.alpha = alpha
            ring.strokeWidth = width * 0.05f
            if (state == "thinking") {
                canvas.drawArc(cx - r * 1.25f, cy - r * 1.25f, cx + r * 1.25f, cy + r * 1.25f, phase * 360f, 250f, false, ring)
            } else {
                canvas.drawCircle(cx, cy, r * grow, ring)
            }
            canvas.drawCircle(cx, cy, r, fill)
        }
    }
}
