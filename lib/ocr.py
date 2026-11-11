from pathlib import Path

import cv2


def read_image(path: Path):
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"Could not read {path.name}")
    return image


def card_count(path: Path) -> int:
    height, width = read_image(path).shape[:2]
    if height < 414 or width < 834:
        raise ValueError(f"Unexpected drop image size {width}x{height}")
    return 4 if width >= 1112 else 3


def write_gray(path: Path, image):
    if not cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)):
        raise OSError(f"Could not write {path.name}")


async def get_card(path: Path, source: Path, index: int):
    left = index * 278
    write_gray(path, read_image(source)[:414, left:left + 278])


async def get_top(source: Path, output: Path):
    write_gray(output, read_image(source)[65:105, 45:230])


async def get_bottom(source: Path, output: Path):
    write_gray(output, read_image(source)[310:365, 45:235])


async def get_print(source: Path, output: Path):
    write_gray(output, read_image(source)[372:385, 145:203])
