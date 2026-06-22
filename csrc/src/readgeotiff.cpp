//  readgeotiff - read a sentinel-1 geotiff slc file and store as complex data
//
//  requires libtiff devel libraries, otherwise compile as:
//
//  gcc -o readgeotiff readgeotiff.c -ltiff
//

#include "tiffio.h"
#include <fstream>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
  std::ofstream outfp;
  int flip, flippix;

  if (argc < 3) {
    printf("Usage: readtiff tifffile slcfile <reverse_line_order y/n "
           "default=n> <reverse pixels y/n def=n>\n");
    exit(0);
  }

  flip = 0; // see if we reverse line order
  if (argc > 3) {
    if (!strncmp(argv[3], "y", 1))
      flip = 1;
  }
  if (flip == 1)
    printf("Reversing line order...\n");
  flippix = 0; // see if we reverse pixel order
  if (argc > 4) {
    if (!strncmp(argv[4], "y", 1))
      flippix = 1;
  }
  if (flippix == 1)
    printf("Reversing pixel order...\n");

  //  open data file for writing
  // outfp=fopen(argv[2],"w");
  outfp.open(argv[2], std::ios::out | std::ios::binary);
  if (!outfp.is_open()) {
    printf("Unable to open file %s\n", argv[2]);
  }

  TIFF *tif = TIFFOpen(argv[1], "r");
  if (tif) {
    uint32_t w, h, i;
    int16_t *buffer;
    int16_t *infilebuffer;
    uint32_t line;
    float *data;

    TIFFGetField(tif, TIFFTAG_IMAGEWIDTH, &w); // get file size info
    TIFFGetField(tif, TIFFTAG_IMAGELENGTH, &h);

    buffer = (int16_t *)_TIFFmalloc(
        TIFFScanlineSize(tif)); // allocate tiff line buffer
    data = (float *)malloc(w * 2 *
                           sizeof(float)); // allocate complex out line buffer

    //  preserving line order case(flag=0)

    if (flip == 0) {
      for (line = 0; line < h; line++) {
        TIFFReadScanline(tif, buffer, line, (tsample_t)1); // read tiff line
        if (flippix == 0) {
          for (i = 0; i < 2 * w; i++) {
            data[i] = buffer[i]; // convert to complex directly
          }
        } else { // reverse complex pixels
          for (i = 0; i < w; i++) {
            data[i * 2] = buffer[w * 2 - 2 - i * 2];
            data[i * 2 + 1] = buffer[w * 2 - 1 - i * 2];
            //		printf("out r, i locs, in r,i locs, w %d %d %d %d
            //%d\n",i*2,i*2+1,w*2-2-i*2,w*2-1-i*2,w);
          }
        }
        outfp.write((char *)data,
                    sizeof(float) * w * 2); // 2 floats per cpx sample
      }
    }

    // reversing line order (flag=1)
    else {
      // printf("bytes %d\n",w*2*h*sizeof(int16_t));
      infilebuffer = (int16_t *)malloc(
          w * 2 * h * sizeof(int16_t)); // allocate full infile buffer
      for (line = 0; line < h; line++) {
        TIFFReadScanline(tif, buffer, line, (tsample_t)1); // read tiff line
        for (i = 0; i < w * 2; i++) {
          infilebuffer[i + line * 2 * w] = buffer[i];
        }
      }
      for (line = 0; line < h; line++) {
        if (flippix == 0) {
          for (i = 0; i < 2 * w; i++) {
            data[i] = infilebuffer[i + (h - line - 1) * 2 *
                                           w]; // convert to complex directly
          }
        } else {
          for (i = 0; i < w;
               i++) //  reverse complex pixels  !! can't test this yet
          {
            data[i * 2] =
                infilebuffer[w * 2 - 2 - i * 2 + (h - line - 1) * 2 * w];
            data[i * 2 + 1] =
                infilebuffer[w * 2 - 1 - i * 2 + (h - line - 1) * 2 * w];
            // data[2*w-1-i]=infilebuffer[i+(h-line-1)*2*w];
          }
        }
        outfp.write((char *)data,
                    sizeof(float) * w * 2); // 2 floats per cpx sample
      }
      free(infilebuffer);
    }

    _TIFFfree(buffer);

    printf("write complex lines, pixels: %d %d\n", w, h);

    TIFFClose(tif);
    free(data);
  }
  outfp.close();

  return 0;
}
