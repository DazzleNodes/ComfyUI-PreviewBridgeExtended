# Manual Test: 3-Layer Architecture Workflow

Tests the LayerCache 3-layer system (upstream, additions, subtractions) across
mode switches and different restore_mask settings.

## Setup

- Load Image node with a masked PNG (mask covers some area, e.g., text)
- PBE node: images <- Load Image.IMAGE, mask_opt <- Load Image.MASK
- Preview Image node: images <- PBE.image
- Convert Mask to Image + Preview Image: mask <- PBE.mask

## Test 1: Basic Combined (Additions Only)

Settings: `{combined, combined, always, never}`

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Queue workflow | Output mask = mask_opt only. Preview shows red tint on masked area. |
| 2 | Open MaskEditor on PBE node, draw new region (not overlapping mask_opt) | MaskEditor shows existing mask_opt as editable. New strokes add to it. |
| 3 | Save MaskEditor | Preview shows red (mask_opt) + orange (new drawing). |
| 4 | Queue workflow | Output mask = mask_opt + new drawing (combined). Both visible in Convert Mask to Image output. |
| 5 | Queue workflow again (no changes) | Same result as step 4. Layers persist (restore_mask=always). |

**Pass criteria**: Step 4 output includes BOTH mask_opt area AND user-drawn area.

## Test 2: Subtraction via Input_Mask Mode

Settings start: `{combined, combined, always, never}`

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Complete Test 1 steps 1-4 first | Have upstream + additions in cache. |
| 2 | Change editor_target to `input_mask` | Preview may update to show only input layer (red). |
| 3 | Open MaskEditor on PBE node | MaskEditor shows only the input mask (mask_opt) in alpha. Your additions are NOT shown (you're editing a different layer). |
| 4 | Erase part of the mask_opt area, Save | Preview should show: reduced red (mask_opt minus subtraction) + orange (additions still there). |
| 5 | Change editor_target back to `combined` | Preview should show: reduced red + orange (all 3 layers visible). |
| 6 | Queue workflow | Output mask = (mask_opt - subtractions) + additions. Should include the additions from Test 1 AND the reduced input area. |

**Pass criteria**: Step 6 output includes additions from Test 1, reduced mask_opt from step 4, and combines both.

## Test 3: Subtraction with restore_mask=never

Settings: `{combined, combined, never, never}`

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Queue workflow (fresh) | Output mask = mask_opt only. |
| 2 | Open MaskEditor (combined mode), draw new region, Save | Preview shows red + orange. |
| 3 | Queue workflow | Output mask = mask_opt + new drawing. |
| 4 | Change editor_target to `input_mask` | |
| 5 | Open MaskEditor, erase part of mask_opt, Save | Preview shows reduced red + orange. |
| 6 | Change back to `{combined, combined, never, never}` | |
| 7 | Queue workflow | **QUESTION**: What SHOULD happen here? |

### Step 7 Analysis

With `restore_mask=never`:
- LayerCache additions/subtractions are cleared at start of process()
- The clipspace file is the LAST MaskEditor save (from step 5, input_mask mode)
- The clipspace contains: mask_opt minus subtractions (NOT the combined state)
- process() re-decomposes this clipspace as mode=combined (current widget)

**Option A** (current behavior): Clipspace (input_mask data) decomposed as "combined":
- additions = clipspace - upstream ≈ 0 (since clipspace < upstream everywhere)
- subtractions = upstream - clipspace = the erased portion
- Result: mask_opt minus subtractions. Additions from step 2 are LOST.
- Rationale: restore_mask=never means "don't keep edits". The clipspace IS the last edit.

**Option B** (alternative): Use last_editor_target to decompose correctly:
- Decompose as input_mask mode: subtractions = upstream - clipspace
- But additions were already cleared by restore_mask=never
- Result: mask_opt minus subtractions. Additions still lost (correctly, per restore=never).

**Both options give the same result for restore_mask=never.** The additions are lost either way because restore_mask=never clears them and the clipspace doesn't contain them.

**Option C**: Raise the question: should restore_mask=never even re-load clipspace at all? If "never" means "don't persist", then maybe clipspace should be ignored too?

## Test 4: Subtraction with restore_mask=always

Settings: `{combined, combined, always, never}`

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1-5 | Same as Test 3 steps 1-5 | Have additions + subtractions in LayerCache. |
| 6 | Change back to `{combined, combined, always, never}` | |
| 7 | Queue workflow | **Expected**: Additions from step 2 PRESERVED (restore_mask=always doesn't clear). Subtractions from step 5 PRESERVED. Output = (mask_opt - subtractions) + additions. |

**Pass criteria**: Step 7 output includes BOTH the additions from step 2 AND the reduced mask_opt from step 5.

---

## Summary: What We're Really Testing

| Scenario | restore_mask | Expected: Additions survive mode switch? |
|----------|-------------|------------------------------------------|
| Test 2 | always | YES - additions preserved across input_mask edit |
| Test 3 | never | NO - additions cleared by restore_mask=never (correct) |
| Test 4 | always | YES - additions preserved, subtractions preserved |

The key question is whether **Test 2 and Test 4** work correctly. If they do, the architecture is sound. Test 3 losing additions is expected behavior with restore_mask=never.
