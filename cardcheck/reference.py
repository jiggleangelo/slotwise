# The two models exercised by the hosted demo were reviewed for the demo rules.
# Other rows remain unverified until a human checks vendor and PCI-ID sources.
REFERENCE = {
    "RTX 3060": {"ids": {"0x2503", "0x2487"}, "vram_gb": 12, "bus_width": 192, "pcie_gen": 4, "pcie_width": 16, "verified": False},
    "RTX 3070": {"ids": {"0x2484"}, "vram_gb": 8, "bus_width": 256, "pcie_gen": 4, "pcie_width": 16, "verified": False},
    "RTX 3080": {"ids": {"0x2206"}, "vram_gb": 10, "bus_width": 320, "pcie_gen": 4, "pcie_width": 16, "verified": False},
    "RTX 3090": {"ids": {"0x2204"}, "vram_gb": 24, "bus_width": 384, "pcie_gen": 4, "pcie_width": 16, "verified": True},
    "RTX 4060": {"ids": {"0x2882"}, "vram_gb": 8, "bus_width": 128, "pcie_gen": 4, "pcie_width": 8, "verified": False},
    "RTX 4070": {"ids": {"0x2786"}, "vram_gb": 12, "bus_width": 192, "pcie_gen": 4, "pcie_width": 16, "verified": False},
    "RTX 4080": {"ids": {"0x2704"}, "vram_gb": 16, "bus_width": 256, "pcie_gen": 4, "pcie_width": 16, "verified": False},
    "RTX 4090": {"ids": {"0x2684"}, "vram_gb": 24, "bus_width": 384, "pcie_gen": 4, "pcie_width": 16, "verified": True},
    "RTX 5070": {"ids": {"0x2f04"}, "vram_gb": 12, "bus_width": 192, "pcie_gen": 5, "pcie_width": 16, "verified": False},
    "RTX 5090": {"ids": {"0x2b85"}, "vram_gb": 32, "bus_width": 512, "pcie_gen": 5, "pcie_width": 16, "verified": False},
}

PARTNER_IDS = {"NVIDIA": "10de", "ASUS": "1043", "MSI": "1462", "GIGABYTE": "1458", "EVGA": "3842"}
