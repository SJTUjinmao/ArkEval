from PIL import Image
import colorsys
import os

def generate_harmonious_gradient(width, height, base_color, output_filename, lightness_offset=0.15):
    """
    Generates a vertical gradient image from a slightly deeper to a slightly lighter shade of the base_color.

    Parameters:
    - width (int): Width of the image.
    - height (int): Height of the image.
    - base_color (tuple): The base color in RGB format, e.g., (255, 0, 0) for red.
    - output_filename (str): The filename to save the generated image.
    - lightness_offset (float): The maximum change in lightness (default is 0.15 for a balanced gradient).
    """

    # Create a new image with RGB mode
    img = Image.new("RGB", (width, height), "#FFFFFF")
    pixels = img.load()

    # Convert base_color from RGB (0-255) to HLS (0-1)
    r, g, b = [x / 255.0 for x in base_color]
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    # Define start and end lightness with moderate offset
    start_l = max(l + lightness_offset, 0.0)  # Slightly deeper at the top
    end_l = min(l, 1.0)    # Slightly lighter at the bottom

    for y in range(height):
        # Calculate the interpolation factor (0 at top, 1 at bottom)
        t = y / (height - 1)
        # Interpolate lightness
        current_l = start_l + (end_l - start_l) * t
        # Convert back to RGB
        current_r, current_g, current_b = colorsys.hls_to_rgb(h, current_l, s)
        # Scale to 0-255 and convert to integers
        current_color = (
            int(current_r * 255),
            int(current_g * 255),
            int(current_b * 255)
        )
        # Set the color for the entire row
        for x in range(width):
            pixels[x, y] = current_color

    # Save the image
    img.save(output_filename)
    print(f"Gradient image saved as {output_filename}")

def main():
    # Define image dimensions
    width = 480
    height = 480

    # Define the list of colors with their names and RGB values
    colors = [
        {"name": "Deep_Rose", "rgb": (175, 23, 64)},
        {"name": "Amethyst_Purple", "rgb": (155, 126, 189)},
        {"name": "Mint_Green", "rgb": (136, 194, 115)},
        {"name": "Bright_Sky_Blue", "rgb": (13, 146, 244)},
        {"name": "Vibrant_Orange", "rgb": (255, 101, 0)},
        {"name": "Periwinkle_Blue", "rgb": (135, 162, 255)},
        {"name": "Soft_Rose", "rgb": (201, 104, 104)},
        {"name": "Flamingo_Pink", "rgb": (240, 90, 126)},
        {"name": "Cream_Yellow", "rgb": (226, 226, 182)},
    ]

    # Create output directory if it doesn't exist
    output_dir = "../features/home/src/main/resources/base/media"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Generate gradient for each color
    for color in colors:
        color_name = color["name"].replace(" ", "_").lower()
        rgb = color["rgb"]
        filename = f"{output_dir}/fallback_{color_name}_gradient.png"
        generate_harmonious_gradient(width, height, rgb, filename)

if __name__ == "__main__":
    main()
