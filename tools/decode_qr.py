"""Decode QR codes from one PDF page; select the right code by visible label."""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--page', type=int, required=True)
    parser.add_argument('--pdftoppm', default='pdftoppm')
    args = parser.parse_args()
    if args.page < 1 or not args.pdf.is_file():
        parser.error('PDF must exist and page must be >= 1')
    try:
        from PIL import Image
        import zxingcpp
    except ImportError:
        parser.exit(2, 'Requires Pillow and zxing-cpp in the chosen Python environment.\n')
    with tempfile.TemporaryDirectory(prefix='canvas-qr-') as temp:
        prefix = Path(temp) / 'page'
        subprocess.run([args.pdftoppm, '-f', str(args.page), '-l', str(args.page),
                        '-r', '240', '-singlefile', '-png', str(args.pdf), str(prefix)], check=True)
        with Image.open(prefix.with_suffix('.png')) as im:
            result = [{'text': code.text, 'position': str(code.position)}
                      for code in zxingcpp.read_barcodes(im)]
    print(json.dumps({'page': args.page, 'codes': result}, ensure_ascii=True, indent=2))

if __name__ == '__main__':
    main()
