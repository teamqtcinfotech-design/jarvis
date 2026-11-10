package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RadialGradient
import android.graphics.Shader
import android.graphics.Typeface
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin

/**
 * JARVIS over whatever is on screen, while a voice-only conversation runs with the app closed.
 *
 * Not a card: a strip of dark glass rises from the bottom edge and fades out upwards. In it, a small reactor
 * core with rings turning around it throws beams of light up across the screen, and glowing voice waves run
 * from edge to edge through it. The waves move with his voice while JARVIS listens, ripple fast while it
 * thinks, and roll while it speaks. The line being said sits underneath. A tap anywhere on it stops JARVIS.
 *
 * Needs "Display over other apps". It cannot appear over the lock screen (Android forbids it); there the
 * voice alone carries on.
 */
class OverlayOrb(private val context: Context, private val onStop: () -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var root: FrameLayout? = null
    private var hud: HudView? = null
    private var status: TextView? = null
    private var line: TextView? = null
    private var shown = false

    private fun dp(v: Float): Int = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, context.resources.displayMetrics).toInt()

    fun canShow(): Boolean = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)

    fun show(text: String, state: String) {
        main.post {
            if (!canShow()) return@post
            if (root == null) build()
            apply(text, state)
            if (!shown) {
                try {
                    val view = root ?: return@post
                    view.alpha = 0f
                    view.translationY = dp(60f).toFloat()
                    wm.addView(view, params())
                    shown = true
                    hud?.start()
                    view.animate().alpha(1f).translationY(0f).setDuration(320).start()  // it rises into place
                } catch (_: Throwable) {
                }
            }
        }
    }

    fun update(text: String?, state: String?) {
        main.post {
            if (shown) apply(text, state)
        }
    }

    /** How loud he is speaking right now (the recogniser's level, about -2 quiet to 10 loud): the waves follow it. */
    fun level(rmsDb: Float) {
        hud?.voice = ((rmsDb + 2f) / 12f).coerceIn(0f, 1f)
    }

    fun hide() {
        main.post {
            if (!shown) return@post
            shown = false
            try {
                hud?.stop()
                root?.animate()?.cancel()
                wm.removeView(root)
            } catch (_: Throwable) {
            }
        }
    }

    private fun apply(text: String?, state: String?) {
        if (text != null) line?.text = if (text == "…") "" else text
        if (state != null) {
            hud?.state = state
            if (state != "listening") hud?.voice = 0f
            status?.text = when (state) {
                "listening" -> "LISTENING"
                "thinking" -> "THINKING"
                else -> "JARVIS"
            }
        }
    }

    private fun params(): WindowManager.LayoutParams {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        return WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            dp(HEIGHT_DP),
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            y = 0
        }
    }

    private fun build() {
        val frame = FrameLayout(context)
        val view = HudView(context)
        frame.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))

        val statusView = TextView(context).apply {
            text = "JARVIS"
            setTextColor(Color.rgb(143, 233, 255))
            textSize = 11f
            letterSpacing = 0.34f
            typeface = Typeface.DEFAULT_BOLD
        }
        frame.addView(statusView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            topMargin = dp(CORE_Y_DP + 74f)
        })

        val lineView = TextView(context).apply {
            setTextColor(Color.WHITE)
            textSize = 16f
            gravity = Gravity.CENTER
            maxLines = 3
            ellipsize = TextUtils.TruncateAt.END
            setLineSpacing(dp(2f).toFloat(), 1f)
            setShadowLayer(10f, 0f, 2f, Color.argb(170, 0, 0, 0))
        }
        frame.addView(lineView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.TOP
            topMargin = dp(CORE_Y_DP + 96f)
            leftMargin = dp(28f)
            rightMargin = dp(28f)
        })

        val hint = TextView(context).apply {
            text = "Tap here to stop"
            setTextColor(Color.argb(150, 255, 255, 255))
            textSize = 11f
        }
        frame.addView(hint, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            bottomMargin = dp(12f)
        })

        frame.setOnClickListener { onStop() }  // "to stop, tap it"
        root = frame
        hud = view
        status = statusView
        line = lineView
    }

    /**
     * Everything that glows, drawn fresh every frame: the glass, two lights drifting along the bottom, the beams,
     * three voice waves and the reactor core with its rings. Plain shapes and gradients only (no blur filters),
     * so it is light enough to run over any app.
     */
    private class HudView(context: Context) : View(context) {
        var state: String = "idle"
        /** 0..1, how loud he is right now (set from the recogniser while listening). */
        @Volatile var voice: Float = 0f

        private val d = resources.displayMetrics.density
        private val began = SystemClock.uptimeMillis()
        private var running = false
        private var level = 0f
        private var cx = 0f
        private var cy = 0f
        private var r = 0f
        private var blobR = 0f

        private val glass = Paint()
        private val light = Paint(Paint.ANTI_ALIAS_FLAG)
        private val beam = Paint(Paint.ANTI_ALIAS_FLAG)
        private val core = Paint(Paint.ANTI_ALIAS_FLAG)
        private val wave = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeCap = Paint.Cap.ROUND
            strokeJoin = Paint.Join.ROUND
        }
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeCap = Paint.Cap.ROUND
        }
        private val path = Path()
        private var blobCyan: Shader? = null
        private var blobViolet: Shader? = null
        private var halo: Shader? = null
        private var heart: Shader? = null

        fun start() {
            running = true
            invalidate()
        }

        fun stop() {
            running = false
        }

        override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
            cx = w / 2f
            cy = CORE_Y_DP * d
            r = 28f * d
            blobR = w * 0.62f
            // Dark glass that is clear at the top edge, so it never reads as a box.
            glass.shader = LinearGradient(
                0f, 0f, 0f, h.toFloat(),
                intArrayOf(Color.argb(0, 8, 9, 30), Color.argb(150, 8, 9, 30), Color.argb(232, 7, 8, 26), Color.argb(248, 5, 6, 20)),
                floatArrayOf(0f, 0.18f, 0.42f, 1f), Shader.TileMode.CLAMP,
            )
            blobCyan = RadialGradient(0f, 0f, blobR, intArrayOf(Color.argb(84, 60, 205, 255), Color.argb(0, 60, 205, 255)), null, Shader.TileMode.CLAMP)
            blobViolet = RadialGradient(0f, 0f, blobR, intArrayOf(Color.argb(78, 150, 100, 255), Color.argb(0, 150, 100, 255)), null, Shader.TileMode.CLAMP)
            // Beams are brightest at the core and gone before they reach the top edge.
            beam.shader = RadialGradient(cx, cy, cy * 1.02f, intArrayOf(Color.argb(150, 150, 232, 255), Color.argb(40, 120, 200, 255), Color.argb(0, 120, 200, 255)),
                floatArrayOf(0f, 0.55f, 1f), Shader.TileMode.CLAMP)
            halo = RadialGradient(0f, 0f, r * 2.7f, intArrayOf(Color.argb(170, 92, 225, 255), Color.argb(46, 92, 190, 255), Color.argb(0, 92, 190, 255)),
                floatArrayOf(0f, 0.5f, 1f), Shader.TileMode.CLAMP)
            heart = RadialGradient(0f, 0f, r, intArrayOf(Color.WHITE, Color.rgb(125, 232, 255), Color.rgb(59, 75, 216), Color.rgb(22, 18, 96)),
                floatArrayOf(0f, 0.36f, 0.8f, 1f), Shader.TileMode.CLAMP)
        }

        private fun rad(deg: Float): Float = (deg * PI / 180.0).toFloat()

        override fun onDraw(canvas: Canvas) {
            val w = width.toFloat()
            val h = height.toFloat()
            if (w <= 0f || h <= 0f) return
            val t = (SystemClock.uptimeMillis() - began) / 1000f
            level += (voice - level) * 0.22f

            canvas.drawRect(0f, 0f, w, h, glass)

            // Two soft lights drifting along the bottom, one cool and one violet.
            light.shader = blobCyan
            canvas.save()
            canvas.translate(w * (0.2f + 0.12f * sin(t * 0.6f)), h * 0.94f)
            canvas.drawCircle(0f, 0f, blobR, light)
            canvas.restore()
            light.shader = blobViolet
            canvas.save()
            canvas.translate(w * (0.8f + 0.12f * sin(t * 0.47f + 2f)), h * 0.9f)
            canvas.drawCircle(0f, 0f, blobR, light)
            canvas.restore()

            // Beams of light thrown up and out from the core; the fan sways and each beam flickers.
            val sway = sin(t * 0.5f) * 9f
            val reach = cy * 1.02f
            val wider = if (state == "speaking") 1.35f else if (state == "thinking") 0.8f else 1f
            for (i in 0 until BEAMS) {
                val a = -90f + (i - (BEAMS - 1) / 2f) * 24f + sway
                val half = (4f + 2.2f * sin(t * 1.3f + i)) * wider
                path.reset()
                path.moveTo(cx, cy)
                path.lineTo(cx + reach * cos(rad(a - half)), cy + reach * sin(rad(a - half)))
                path.lineTo(cx + reach * cos(rad(a + half)), cy + reach * sin(rad(a + half)))
                path.close()
                beam.alpha = (105 + 70 * sin(t * 0.9f + i * 1.7f) + 60 * level).toInt().coerceIn(25, 220)
                canvas.drawPath(path, beam)
            }

            // Voice waves, edge to edge through the core.
            val amp = when (state) {
                "listening" -> (9f + 36f * level) * d
                "speaking" -> (17f + 7f * sin(t * 6f)) * d
                "thinking" -> 6f * d
                else -> 5f * d
            }
            val speed = when (state) {
                "thinking" -> 6f
                "speaking" -> 3.2f
                "listening" -> 2.4f
                else -> 1.1f
            }
            drawWave(canvas, w, t * speed, amp, 1.6f, Color.rgb(92, 225, 255))
            drawWave(canvas, w, t * speed * 0.8f + 1.3f, amp * 0.72f, 2.3f, Color.rgb(160, 126, 255))
            drawWave(canvas, w, t * speed * 1.25f + 2.6f, amp * 0.46f, 3.1f, Color.WHITE)

            // The reactor: a halo, a fine outer ring with ticks, two broken rings turning opposite ways, the core.
            val tint = when (state) {
                "thinking" -> Color.rgb(176, 138, 255)
                "speaking" -> Color.rgb(126, 196, 255)
                else -> Color.rgb(92, 225, 255)
            }
            val spin = when (state) {
                "thinking" -> 280f
                "speaking" -> 120f
                "listening" -> 76f
                else -> 36f
            }
            val pulse = 1f + 0.05f * sin(t * 2.2f) + 0.22f * level
            light.shader = halo
            canvas.save()
            canvas.translate(cx, cy)
            canvas.scale(pulse, pulse)
            canvas.drawCircle(0f, 0f, r * 2.7f, light)
            canvas.restore()

            ring.color = tint
            ring.alpha = 70
            ring.strokeWidth = 1f * d
            canvas.drawCircle(cx, cy, r * 1.98f, ring)
            ring.alpha = 160
            ring.strokeWidth = 1.3f * d
            for (i in 0 until 24) {
                val a = rad(i * 15f + t * spin * 0.2f)
                val inner = r * 1.98f
                val outer = r * (if (i % 6 == 0) 2.16f else 2.06f)
                canvas.drawLine(cx + inner * cos(a), cy + inner * sin(a), cx + outer * cos(a), cy + outer * sin(a), ring)
            }
            ring.alpha = 235
            ring.strokeWidth = 2.4f * d
            arcs(canvas, r * 1.62f, t * spin, 3, 84f)
            ring.color = Color.rgb(178, 150, 255)
            ring.alpha = 225
            ring.strokeWidth = 3.2f * d
            arcs(canvas, r * 1.3f, -t * spin * 1.4f, 4, 46f)

            core.shader = heart
            core.alpha = 255
            canvas.save()
            canvas.translate(cx, cy)
            canvas.drawCircle(0f, 0f, r, core)
            canvas.restore()
            core.shader = null
            core.color = Color.WHITE
            core.alpha = (165 + 90 * level).toInt().coerceAtMost(255)
            canvas.drawCircle(cx, cy, r * (0.26f + 0.16f * level + 0.03f * sin(t * 4f)), core)

            if (running) postInvalidateOnAnimation()
        }

        private fun arcs(canvas: Canvas, radius: Float, startDeg: Float, count: Int, sweep: Float) {
            val step = 360f / count
            for (i in 0 until count) {
                canvas.drawArc(cx - radius, cy - radius, cx + radius, cy + radius, startDeg + i * step, sweep, false, ring)
            }
        }

        /** One sine line that dies away at both edges, drawn three times (wide and faint to thin and bright) so it glows. */
        private fun drawWave(canvas: Canvas, w: Float, phase: Float, amp: Float, cycles: Float, color: Int) {
            path.reset()
            for (i in 0..WAVE_POINTS) {
                val u = i / WAVE_POINTS.toFloat()
                val window = sin(PI * u).toFloat().pow(1.5f)
                val y = cy + amp * window * sin(u * cycles * 2f * PI.toFloat() + phase)
                if (i == 0) path.moveTo(0f, y) else path.lineTo(u * w, y)
            }
            wave.color = color
            wave.strokeWidth = 9f * d
            wave.alpha = 24
            canvas.drawPath(path, wave)
            wave.strokeWidth = 4.5f * d
            wave.alpha = 62
            canvas.drawPath(path, wave)
            wave.strokeWidth = 1.6f * d
            wave.alpha = 235
            canvas.drawPath(path, wave)
        }
    }

    companion object {
        private const val HEIGHT_DP = 316f   // the whole strip (room for three lines of reply above the hint)
        private const val CORE_Y_DP = 104f   // where the core sits, from the top of the strip
        private const val BEAMS = 7
        private const val WAVE_POINTS = 56
    }
}
