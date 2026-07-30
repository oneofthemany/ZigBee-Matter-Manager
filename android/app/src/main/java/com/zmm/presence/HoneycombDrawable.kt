package com.zmm.presence

import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.Shader
import android.graphics.drawable.Drawable
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.sin

/**
 * The honeycomb backdrop: base fill, a tiled grid of hex outlines, and a honey
 * glow bleeding down from the top.
 *
 * Drawn in code rather than declared as a resource because a tiling background
 * needs a BitmapDrawable, and a bitmap is the wrong thing to ship for this — it
 * would need a density ladder of PNGs, and the stroke colour has to change
 * between light and night. A path costs nothing to rasterise once and stays
 * crisp at any size.
 *
 * Geometry matches --hexclip in hive-tokens.css: points at left and right, flat
 * top and bottom. Columns therefore advance by 3/4 of a cell width and every
 * other column drops half a cell, which is what interlocks them.
 */
class HoneycombDrawable(
    private val baseColor: Int,
    private val strokeColor: Int,
    private val glowColor: Int,
    private val cellRadius: Float,
    private val strokeWidthPx: Float,
) : Drawable() {

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = baseColor
    }

    private val combPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = strokeColor
        strokeWidth = strokeWidthPx
    }

    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    /**
     * The whole grid as one path, rebuilt only when the view resizes.
     *
     * Cells share edges, so this deliberately redraws each boundary twice
     * rather than tracking which ones are already stroked. At this alpha the
     * doubling is invisible, and the bookkeeping to avoid it would cost more
     * than the overdraw.
     */
    private val comb = Path()

    override fun onBoundsChange(bounds: Rect) {
        rebuild(bounds)
    }

    private fun rebuild(bounds: Rect) {
        comb.reset()

        val r = cellRadius
        val h = (Math.sqrt(3.0) * r).toFloat()   // full height, point to point
        val dx = 1.5f * r                        // horizontal column pitch

        // Start a column and a row outside the view so partial cells at the
        // edges look cut off by the frame rather than stopping short of it.
        val cols = ceil(bounds.width() / dx).toInt() + 2
        val rows = ceil(bounds.height() / h).toInt() + 2

        for (col in -1 until cols) {
            val cx = bounds.left + col * dx
            val yOffset = if (col % 2 == 0) 0f else h / 2f
            for (row in -1 until rows) {
                val cy = bounds.top + row * h + yOffset
                addHex(cx, cy, r)
            }
        }
    }

    private fun addHex(cx: Float, cy: Float, r: Float) {
        for (i in 0 until 6) {
            val a = Math.toRadians(60.0 * i)
            val x = cx + r * cos(a).toFloat()
            val y = cy + r * sin(a).toFloat()
            if (i == 0) comb.moveTo(x, y) else comb.lineTo(x, y)
        }
        comb.close()
    }

    override fun draw(canvas: Canvas) {
        val b = bounds
        canvas.drawRect(b, fillPaint)
        canvas.drawPath(comb, combPaint)

        // Glow last, over the comb, so the cells near the top fade into it
        // instead of ruling lines across it.
        if (glowPaint.shader == null && b.height() > 0) {
            glowPaint.shader = LinearGradient(
                0f, b.top.toFloat(), 0f, b.top + b.height() * 0.45f,
                glowColor, Color.TRANSPARENT, Shader.TileMode.CLAMP,
            )
        }
        canvas.drawRect(b, glowPaint)
    }

    override fun setAlpha(alpha: Int) {
        combPaint.alpha = alpha
    }

    override fun setColorFilter(colorFilter: ColorFilter?) {
        combPaint.colorFilter = colorFilter
    }

    @Deprecated("Required by Drawable; PixelFormat is legacy API.")
    override fun getOpacity(): Int = PixelFormat.OPAQUE
}
