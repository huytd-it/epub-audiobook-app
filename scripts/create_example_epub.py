"""Create the small Vietnamese EPUB fixture shipped with the repository."""
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


OUTPUT = Path(__file__).resolve().parents[1] / "data/examples/mau-kiem-thu-4-chuong.epub"
CHAPTERS = [
    (
        "CHƯƠNG 01 — CHIẾC CHÌA KHÓA ĐỎ",
        "Sáng thứ Hai, An tìm thấy một chiếc chìa khóa đỏ trên bậc thềm thư viện. "
        "Trên móc chìa khóa có tấm thẻ ghi rõ: Phòng đọc phía Đông. An cất nó vào túi "
        "và quyết định hỏi cô thủ thư sau giờ học. Ngoài sân, chiếc đồng hồ vừa điểm "
        "tám tiếng, còn con mèo mướp nằm im bên chậu hoa.",
    ),
    (
        "CHƯƠNG 02 — BỨC THƯ TRONG NGĂN BÀN",
        "Buổi trưa, cô thủ thư đưa An đến căn phòng phía Đông. Cánh cửa mở ra bằng "
        "chiếc chìa khóa đỏ. Trong ngăn bàn gỗ có một bức thư màu xanh, bên ngoài "
        "viết tên người nhận là Bạn đọc tiếp theo. An nhẹ nhàng mở thư và đọc dòng "
        "đầu tiên: Nếu bạn đang cầm lá thư này, hãy mang nó đến ga tàu cũ.",
    ),
    (
        "CHƯƠNG 03 — CHUYẾN TÀU LÚC BA GIỜ",
        "Đúng ba giờ chiều, An đến ga tàu cũ cùng người bạn tên Bình. Sân ga phủ đầy "
        "lá vàng, nhưng chiếc ghế số bảy vẫn sạch sẽ. Dưới ghế, hai bạn tìm thấy một "
        "chiếc hộp nhỏ có hình ngôi sao. Bên trong hộp là tấm bản đồ dẫn tới ngọn hải "
        "đăng ở cuối con đường ven biển.",
    ),
    (
        "CHƯƠNG 04 — NGỌN ĐÈN CUỐI ĐƯỜNG",
        "Khi trời vừa tối, An và Bình tới ngọn hải đăng. Người gác đèn đón hai bạn "
        "bằng nụ cười hiền hậu và kể rằng đây là trò truy tìm kho báu dành cho những "
        "người ham đọc. Món quà cuối cùng là một cuốn sổ trắng để mỗi bạn viết câu "
        "chuyện của mình. An viết về chiếc chìa khóa đỏ, còn Bình đặt tên cho truyện "
        "là Bốn Dấu Mốc.",
    ),
]


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    manifest = "".join(
        f'<item id="c{i}" href="chapter-{i:02}.xhtml" '
        'media-type="application/xhtml+xml"/>'
        for i in range(1, len(CHAPTERS) + 1)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(CHAPTERS) + 1))
    nav_links = "".join(
        f'<li><a href="chapter-{i:02}.xhtml">{escape(title)}</a></li>'
        for i, (title, _) in enumerate(CHAPTERS, 1)
    )
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="uid" xml:lang="vi">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">example-vietnamese-four-chapters</dc:identifier>'
        '<dc:title>Bốn Dấu Mốc - Ebook mẫu kiểm thử</dc:title>'
        '<dc:language>vi</dc:language><dc:creator>Ebook mẫu</dc:creator>'
        '<dc:description>Ebook tiếng Việt ngắn, gồm bốn chương có tiêu đề riêng '
        'để kiểm thử nhận diện và sắp xếp chương.</dc:description></metadata>'
        f'<manifest><item id="nav" href="nav.xhtml" '
        f'media-type="application/xhtml+xml" properties="nav"/>{manifest}</manifest>'
        f'<spine>{spine}</spine></package>'
    )
    with ZipFile(OUTPUT, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
        archive.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" lang="vi">'
            f'<head><title>Mục lục</title></head><body><nav epub:type="toc">'
            f'<h1>Mục lục</h1><ol>{nav_links}</ol></nav></body></html>',
            compress_type=ZIP_DEFLATED,
        )
        for i, (title, body) in enumerate(CHAPTERS, 1):
            xhtml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml" lang="vi">'
                f'<head><title>{escape(title)}</title></head>'
                f'<body><h1>{escape(title)}</h1><p>{escape(body)}</p></body></html>'
            )
            archive.writestr(f"OEBPS/chapter-{i:02}.xhtml", xhtml, compress_type=ZIP_DEFLATED)
    print(OUTPUT)


if __name__ == "__main__":
    main()
