"""Run explicitly by the user; downloads model weights, never uploads video."""
from pathlib import Path
from transformers import CLIPModel, CLIPProcessor

target = Path(__file__).with_name('models') / 'clip'
target.mkdir(parents=True, exist_ok=True)
print('Pobieranie lokalnego modelu CLIP. Może potrwać kilka minut.', flush=True)
model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32', use_safetensors=False, weights_only=True)
processor = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
model.save_pretrained(target, safe_serialization=True)
processor.save_pretrained(target)
(target / 'ready.txt').write_text('CLIP installed', encoding='utf-8')
print('Gotowe. Uruchom ponownie aplikację.')
