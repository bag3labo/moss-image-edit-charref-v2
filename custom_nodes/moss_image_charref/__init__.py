"""ComfyUI nodes for the MOSS-Image character reference LoRA (moss-image-edit-charref-v2).

MOSS character reference layout
    ComfyUI places a reference image on the same position grid as the output. This LoRA was trained with the reference
    one output height below the output instead, so the two never share a position. Connect the model and the empty
    latent you sample from; the node reads the output height from the latent.

MOSS character reference guider
    Optional. Sampling with one extra pass that has no reference, to strengthen what the reference adds:
    no_ref + reference_guidance * (negative - no_ref) + cfg * (positive - negative).
    reference_guidance 1.0 is the plain sampling. The skip-layer guidance of the official MOSS-Image sampling is built
    in (block 22 on both negative passes), so do not add a Skip Layer Guidance node in front of it.

Install: copy this folder into ComfyUI/custom_nodes/ and restart ComfyUI. It needs no extra packages.
"""
import comfy.ldm.flux.math
import comfy.model_patcher
import comfy.samplers
import torch


class MossCharRefLayout:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "latent": ("LATENT",)}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "moss"

    def patch(self, model, latent):
        target_rows = int(latent["samples"].shape[-2])  # a MOSS-Image latent row is 16 pixels, one position
        patched = model.clone()
        dit = patched.get_model_object("diffusion_model")
        original = dit.rope

        def rope(h, w, text_len, device, transformer_options={}, ref_shapes=(), f_offset=0):
            pe = original(h, w, text_len, device, transformer_options, ref_shapes, f_offset)
            start = h * w + text_len
            for i, (rh, rw) in enumerate(ref_shapes):
                n = rh * rw
                ids = torch.zeros(1, n, 4, device=device, dtype=torch.float32)
                ids[0, :, 0] = i + 1
                # with the "ai-toolkit" edit format the references are encoded once, before sampling, with h = 0
                ids[0, :, 2] = (torch.arange(rh, device=device, dtype=torch.float32) - (rh - 1) / 2 + (h or target_rows)).repeat_interleave(rw)
                ids[0, :, 3] = (torch.arange(rw, device=device, dtype=torch.float32) - (rw - 1) / 2).repeat(rh)
                moved = torch.cat([comfy.ldm.flux.math.rope(ids[..., k], d, 10000) for k, d in enumerate(dit.rope_axes_dim)], dim=-3)
                pe[:, start:start + n] = moved.unsqueeze(2).to(pe.dtype)
                start += n
            return pe

        patched.add_object_patch("diffusion_model.rope", rope)
        return (patched,)


class CharRefGuider(comfy.samplers.CFGGuider):
    def set_params(self, cfg, reference_guidance, skip_layer):
        self.cfg, self.reference_guidance, self.skip_layer = cfg, reference_guidance, skip_layer

    def predict_noise(self, x, timestep, model_options={}, seed=None):
        def skip(args, extra_args):
            return args

        skipped = model_options
        if self.skip_layer >= 0:
            skipped = comfy.model_patcher.set_model_options_patch_replace(model_options.copy(), skip, "dit", "single_block", self.skip_layer)
        calc = comfy.samplers.calc_cond_batch
        cond = calc(self.inner_model, [self.conds["positive"]], x, timestep, model_options)[0]
        uncond = calc(self.inner_model, [self.conds["negative"]], x, timestep, skipped)[0]
        guided = uncond + self.cfg * (cond - uncond)
        if self.reference_guidance == 1.0:
            return guided
        no_ref = calc(self.inner_model, [self.conds["negative_no_reference"]], x, timestep, skipped)[0]
        return guided + (self.reference_guidance - 1.0) * (uncond - no_ref)


class MossCharRefGuider:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "positive": ("CONDITIONING",), "negative": ("CONDITIONING",),
                             "negative_no_reference": ("CONDITIONING",),
                             "cfg": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                             "reference_guidance": ("FLOAT", {"default": 2.5, "min": 0.0, "max": 20.0, "step": 0.05}),
                             "skip_layer": ("INT", {"default": 22, "min": -1, "max": 1000})}}

    RETURN_TYPES = ("GUIDER",)
    FUNCTION = "get_guider"
    CATEGORY = "moss"

    def get_guider(self, model, positive, negative, negative_no_reference, cfg, reference_guidance, skip_layer):
        guider = CharRefGuider(model)
        guider.inner_set_conds({"positive": positive, "negative": negative, "negative_no_reference": negative_no_reference})
        guider.set_params(cfg, reference_guidance, skip_layer)
        return (guider,)


NODE_CLASS_MAPPINGS = {"MossCharRefLayout": MossCharRefLayout, "MossCharRefGuider": MossCharRefGuider}
NODE_DISPLAY_NAME_MAPPINGS = {"MossCharRefLayout": "MOSS character reference layout", "MossCharRefGuider": "MOSS character reference guider"}
