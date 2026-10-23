"""Optional local CLIP inference. No downloads during analysis."""
from pathlib import Path
import importlib.util
import numpy as np

MODEL = Path(__file__).with_name('models') / 'clip'
CATEGORIES = {
    'action': ('Dynamiczna akcja', 'a photo of dynamic sports action'),
    'people': ('Ludzie', 'a photo of people'),
    'animals': ('Zwierzęta', 'a photo of an animal'),
    'landscape': ('Krajobrazy', 'a photo of a natural landscape'),
    'architecture': ('Architektura', 'a photo of buildings and architecture'),
    'attractions': ('Atrakcje', 'a photo of a tourist landmark'),
    'details': ('Zbliżenia', 'a close up photo of an object'),
    'vehicles': ('Pojazdy', 'a photo of vehicles'),
    'food': ('Jedzenie', 'a photo of food and drinks'),
    'performance': ('Koncerty i występy', 'a photo of musicians performing on stage'),
    'water': ('Woda i plaża', 'a photo of a lake sea or beach'),
    'snow': ('Śnieg i góry', 'a photo of snowy mountains'),
}


def available():
    return (MODEL / 'ready.txt').exists() and all(importlib.util.find_spec(x) is not None for x in ('torch', 'transformers'))


class Classifier:
    def __init__(self):
        if not available():
            raise ValueError('Kategorie wymagają lokalnego modelu. Uruchom Zainstaluj-kategorie.cmd, potem ponownie aplikację.')
        from transformers import CLIPModel, CLIPProcessor
        self.model = CLIPModel.from_pretrained(str(MODEL), local_files_only=True, use_safetensors=True).eval()
        self.processor = CLIPProcessor.from_pretrained(str(MODEL), local_files_only=True)

    def classify(self, images):
        import torch
        prompts = [v[1] for v in CATEGORIES.values()] + ['a blurry or unrecognizable image']
        inputs = self.processor(text=prompts, images=images, return_tensors='pt', padding=True)
        with torch.inference_mode():
            output = self.model(**inputs)
            # Cosine similarity, not a calibrated probability of a category.
            values = (output.logits_per_image / self.model.logit_scale.exp()).cpu().numpy()
        support = np.median(values, axis=0)
        best = float(max(support[:-1]))
        labels = [key for key, value in zip(CATEGORIES, support[:-1])
                  if value >= .23 and value >= best - .035 and value > support[-1] + .015]
        return labels or ['unknown'], {key: round(float(value), 3) for key, value in zip(CATEGORIES, support[:-1])}
