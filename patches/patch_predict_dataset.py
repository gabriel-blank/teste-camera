from pathlib import Path
import anomalib.data.predict as predict_mod
from anomalib.data import ImageItem
from anomalib.data.utils import get_image_filenames, read_image
from torch.utils.data.dataset import Dataset
from torchvision.transforms.v2 import Transform
from collections.abc import Callable
from datetime import datetime
import torch


class PatchedPredictDataset(Dataset):
    def __init__(
        self,
        path: str | Path = None,
        transform: Transform | None = None,
        image_size: int | tuple[int, int] = (256, 256),
        images: list[torch.Tensor] | None = None,
    ) -> None:
        super().__init__()

        self.transform = transform
        self.image_size = image_size
        self.images_in_memory = images
        self.timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")

        if images is not None:
            self.image_filenames = [f"{self.timestamp}.png" for _ in range(len(images))]
        else:
            self.image_filenames = get_image_filenames(path)

    def __len__(self) -> int:
        return len(self.image_filenames)

    def __getitem__(self, index: int) -> ImageItem:
        image_filename = self.image_filenames[index]
        if self.images_in_memory is not None:
            image = self.images_in_memory[index]
        else:
            image = read_image(image_filename, as_tensor=True)

        if self.transform:
            image = self.transform(image)

        return ImageItem(
            image=image,
            image_path=f"{self.timestamp}.png",
        )

    @property
    def collate_fn(self) -> Callable:
        from anomalib.data import ImageBatch

        return ImageBatch.collate


predict_mod.PredictDataset = PatchedPredictDataset
