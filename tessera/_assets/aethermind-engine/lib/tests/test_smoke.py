"""
Smoke test for AetherMind Python library
"""

import os
import tempfile
import shutil
from aethermind import AetherMind, AetherLayer

def test_smoke():
    """Test basic functionality of AetherMind library"""
    # Create temporary directory
    temp_dir = tempfile.mkdtemp()
    try:
        # Initialize AetherMind context
        aether = AetherMind(temp_dir, create=True)

        # Write a layer
        written = aether.write_layer(
            type="load-bearing",
            body="!This is a test layer",
            ctx="test/smoke",
            conf=0.95,
            markers=["!"]
        )

        # Write a texture entry
        aether.write_texture(
            body="~This is a test texture entry",
            ctx="test/smoke",
            marker="~"
        )

        # Read back the layer
        layers = aether.read_layers(ctx="test/smoke")
        assert len(layers) == 1, f"Expected 1 layer, got {len(layers)}"
        layer = layers[0]
        assert layer.type == "load-bearing", f"Expected type 'load-bearing', got {layer.type}"
        assert layer.ctx == "test/smoke", f"Expected ctx 'test/smoke', got {layer.ctx}"
        assert layer.conf == 0.95, f"Expected conf 0.95, got {layer.conf}"
        assert layer.body == "!This is a test layer", f"Expected body '!This is a test layer', got {layer.body}"
        assert layer.id == written.id, f"Expected id {written.id}, got {layer.id}"

        # Read back the texture
        textures = aether.read_texture()
        assert "[unknown | test/smoke]" in textures
        assert "~This is a test texture entry" in textures
        assert textures.endswith("---\n")

        print("All tests passed!")

    finally:
        # Clean up
        shutil.rmtree(temp_dir)

if __name__ == "__main__":
    test_smoke()
