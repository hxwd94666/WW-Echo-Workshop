# 规范化声骸词条和繁简名称。
"""Normalize game OCR text before matching Echo UI and scoring templates."""

# Domain labels use deterministic spelling normalization. Full name conversion
# happens at the application input boundary before domain calculation.
_ECHO_TRADITIONAL = str.maketrans(
    {
        '聲': '声',
        '強': '强',
        '並': '并',
        '調': '调',
        '諧': '谐',
        '鳴': '鸣',
        '裝': '装',
        '備': '备',
        '戰': '战',
        '擊': '击',
        '傷': '伤',
        '療': '疗',
        '禦': '御',
        '護': '护',
        '體': '体',
        '屬': '属',
        '導': '导',
        '電': '电',
        '氣': '气',
        '動': '动',
        '滅': '灭',
        '熱': '热',
        '釋': '释',
        '寧': '宁',
        '婭': '娅',
        '達': '达',
        '貝': '贝',
        '霽': '霁',
        '聖': '圣',
        '蘿': '萝',
        '麗': '丽',
        '爾': '尔',
        '維': '维',
        '愛': '爱',
        '彌': '弥',
        '擇': '择',
        '選': '选',
        '項': '项',
        '檔': '档',
        '滿': '满',
        '塵': '尘',
        '級': '级',
        # Header labels and set candidates also pass through this fallback. Without
        # these glyphs a labelled name can be mistaken for a bare name, 簡述 for a
        # second header, and the same set for two different observations. This is
        # spelling normalization only, not an Echo/character-name correction table.
        '稱': '称',
        '簡': '简',
        '數': '数',
        '隱': '隐',
        '迴': '回',
        '輕': '轻',
        '雲': '云',
        '毀': '毁',
        '異': '异',
    }
)


def simplify_echo_text(text):
    """Convert Traditional Chinese OCR to the Simplified labels used by XWUID."""
    # PaddleOCR sometimes uses the Japanese glyph 撃 for 擊 in Traditional
    # Chinese stat labels (暴撃 / 攻撃). OpenCC does not normalize that glyph.
    text = str(text).replace('撃', '擊')
    return text.translate(_ECHO_TRADITIONAL)
