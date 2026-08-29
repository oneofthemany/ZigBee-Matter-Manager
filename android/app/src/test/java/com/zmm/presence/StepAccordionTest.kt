package com.zmm.presence

import com.zmm.presence.StepAccordion.Step
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The accordion's wrong answers are all invisible on a phone until you retrace
 * the exact path that produces them — a step that reopens and snaps shut, an
 * advance onto a disabled control, a screen that sits on a finished section.
 * Each of those is one call sequence here.
 */
class StepAccordionTest {

    private fun done(vararg finished: Step): Map<Step, Boolean> =
        Step.values().associateWith { it in finished }

    @Test fun `opens the first step on a fresh install`() {
        assertEquals(Step.HUB, StepAccordion().resolve(done()))
    }

    @Test fun `skips finished steps`() {
        assertEquals(Step.GEOFENCE, StepAccordion().resolve(done(Step.HUB, Step.PERMISSIONS)))
    }

    @Test fun `finishing the open step advances to the next`() {
        val a = StepAccordion()
        assertEquals(Step.HUB, a.resolve(done()))
        assertEquals(Step.PERMISSIONS, a.resolve(done(Step.HUB)))
    }

    @Test fun `finishing a hand-opened step advances`() {
        val a = StepAccordion()
        a.resolve(done())
        a.toggle(Step.GEOFENCE)                       // jumped ahead by hand
        assertEquals(Step.GEOFENCE, a.resolve(done()))
        assertEquals(Step.HUB, a.resolve(done(Step.GEOFENCE)))
    }

    /** The bug the transition check exists for: this used to close instantly. */
    @Test fun `reopening an already-finished step keeps it open`() {
        val a = StepAccordion()
        val all = done(Step.HUB, Step.PERMISSIONS, Step.GEOFENCE, Step.DRIVE)
        assertNull(a.resolve(all))                    // everything collapsed
        a.toggle(Step.HUB)                            // go back to change the URL
        assertEquals(Step.HUB, a.resolve(all))
        assertEquals(Step.HUB, a.resolve(all))        // and it stays open
    }

    @Test fun `everything collapses once every step is finished`() {
        val a = StepAccordion()
        assertNull(a.resolve(done(Step.HUB, Step.PERMISSIONS, Step.GEOFENCE, Step.DRIVE)))
    }

    @Test fun `tapping the open step closes it and nothing springs open`() {
        val a = StepAccordion()
        assertEquals(Step.HUB, a.resolve(done()))
        a.toggle(Step.HUB)
        assertNull(a.resolve(done()))
    }

    @Test fun `a blocked step is never auto-advanced onto`() {
        val a = StepAccordion()
        val blocked = setOf(Step.DRIVE)
        val everythingElse = done(Step.HUB, Step.PERMISSIONS, Step.GEOFENCE)
        assertNull(a.resolve(everythingElse, blocked))
    }

    @Test fun `a blocked step still opens when its header is tapped`() {
        val a = StepAccordion()
        val blocked = setOf(Step.DRIVE)
        val everythingElse = done(Step.HUB, Step.PERMISSIONS, Step.GEOFENCE)
        a.resolve(everythingElse, blocked)
        a.toggle(Step.DRIVE)
        assertEquals(Step.DRIVE, a.resolve(everythingElse, blocked))
    }

    @Test fun `undoing a step reopens it`() {
        val a = StepAccordion()
        val all = done(Step.HUB, Step.PERMISSIONS, Step.GEOFENCE, Step.DRIVE)
        assertNull(a.resolve(all))
        // Disarming the geofence is a step becoming unfinished again.
        assertEquals(Step.GEOFENCE, a.resolve(done(Step.HUB, Step.PERMISSIONS, Step.DRIVE)))
    }
}
