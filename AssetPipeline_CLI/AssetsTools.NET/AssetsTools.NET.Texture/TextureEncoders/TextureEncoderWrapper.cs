using System;
using System.Runtime.InteropServices;

namespace AssetsTools.NET.Texture
{
    public static partial class TextureEncoderWrapper
    {
        // Cuttlefish and its codec backends use process-wide native state.
        // Imports may process multiple bundles in parallel, so keep each
        // load/convert/free sequence atomic to avoid native memory races.
        private static readonly object NativeEncoderLock = new object();

        [StructLayout(LayoutKind.Sequential)]
        private struct TextureDataMip
        {
            public int Width;
            public int Height;
            public int Size;
            public IntPtr Data;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TextureDataBuffer
        {
            public int Width;
            public int Height;
            public int MipCount;
            public IntPtr Mips;
        }

        [DllImport("textureencoder", CallingConvention = CallingConvention.Cdecl)]
        private static extern int SanityCheck(int number);

        // todo: cuttlefish uses freeimage which uses fopen
        // strings will be not support non-ansi until we patch
        // cuttlefish to use freeimage's windows overload
        [DllImport("textureencoder", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private static extern IntPtr LoadTextureFromFile(string path, int mips = 1);

        [DllImport("textureencoder", CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr LoadTextureFromBuffer(byte[] data, int size, int width, int height, int mips = 1);

        [DllImport("textureencoder", CallingConvention = CallingConvention.Cdecl)]
        private static extern TextureDataBuffer ConvertAndFreeTexture(IntPtr image, TextureFormat format, int quality = 3);

        [DllImport("textureencoder", CallingConvention = CallingConvention.Cdecl)]
        private static extern void FreeTextureDataBuffer(IntPtr mips, int mipCount);

        [DllImport("PVRTexLib", CallingConvention = CallingConvention.Cdecl)]
        private static extern uint PVRTexLib_GetFormatBitsPerPixel(ulong u64PixelFormat);

        private const ulong RGBA8888 =
            (byte)'r' + ((byte)'g' << 8) + ((byte)'b' << 16) + ((byte)'a' << 24) +
            (8L << 32) + (8L << 40) + (8L << 48) + (8L << 56);

        public static byte[][] ConvertImage(
            string path, int mipCount, TextureFormat textureFormat,
            out int width, out int height,
            int quality = 3)
        {
            if (IsPvrtlFormat(textureFormat) && PvrtlStubInUse())
            {
                width = 0;
                height = 0;
                return null;
            }

            lock (NativeEncoderLock)
            {
                IntPtr image = LoadTextureFromFile(path, mipCount);
                if (image == IntPtr.Zero)
                    throw new Exception($"{nameof(LoadTextureFromFile)} returned null.");

                return DoConversion(image, textureFormat, quality, out width, out height);
            }
        }

        public static byte[] ConvertImageFlat(
            string path, int mipCount, TextureFormat textureFormat,
            out int width, out int height,
            int quality = 3)
        {
            if (IsPvrtlFormat(textureFormat) && PvrtlStubInUse())
            {
                width = 0;
                height = 0;
                return null;
            }

            lock (NativeEncoderLock)
            {
                IntPtr image = LoadTextureFromFile(path, mipCount);
                if (image == IntPtr.Zero)
                    throw new Exception($"{nameof(LoadTextureFromFile)} returned null.");

                return DoConversionFlat(image, textureFormat, quality, out width, out height);
            }
        }

        public static byte[][] ConvertImage(
            byte[] rgbaData, int mipCount, TextureFormat textureFormat,
            int width, int height,
            int quality = 3)
        {
            if (IsPvrtlFormat(textureFormat) && PvrtlStubInUse())
            {
                width = 0;
                height = 0;
                return null;
            }

            lock (NativeEncoderLock)
            {
                return ConvertRawMipChainLocked(
                    rgbaData, mipCount, textureFormat, width, height, quality);
            }
        }

        public static byte[] ConvertImageFlat(
            byte[] rgbaData, int mipCount, TextureFormat textureFormat,
            int width, int height,
            int quality = 3)
        {
            if (IsPvrtlFormat(textureFormat) && PvrtlStubInUse())
            {
                width = 0;
                height = 0;
                return null;
            }

            byte[][] mipData;
            lock (NativeEncoderLock)
            {
                mipData = ConvertRawMipChainLocked(
                    rgbaData, mipCount, textureFormat, width, height, quality);
            }

            int totalSize = 0;
            foreach (byte[] mip in mipData)
                totalSize = checked(totalSize + mip.Length);

            byte[] flattened = new byte[totalSize];
            int offset = 0;
            foreach (byte[] mip in mipData)
            {
                Buffer.BlockCopy(mip, 0, flattened, offset, mip.Length);
                offset += mip.Length;
            }
            return flattened;
        }

        private static byte[][] ConvertRawMipChainLocked(
            byte[] bgraData, int requestedMipCount, TextureFormat textureFormat,
            int width, int height, int quality)
        {
            if (bgraData == null)
                throw new ArgumentNullException(nameof(bgraData));
            if (width <= 0)
                throw new ArgumentOutOfRangeException(nameof(width));
            if (height <= 0)
                throw new ArgumentOutOfRangeException(nameof(height));

            long expectedSize = (long)width * height * 4;
            if (expectedSize > int.MaxValue || bgraData.Length != expectedSize)
            {
                throw new ArgumentException(
                    $"Raw BGRA32 buffer length mismatch: got {bgraData.Length}, " +
                    $"expected {expectedSize} for {width}x{height}.",
                    nameof(bgraData));
            }

            int maxMipCount = 1;
            for (int mipWidth = width, mipHeight = height;
                 mipWidth > 1 || mipHeight > 1;
                 mipWidth = Math.Max(1, mipWidth / 2),
                 mipHeight = Math.Max(1, mipHeight / 2))
            {
                maxMipCount++;
            }

            int mipCount = Math.Max(1, Math.Min(requestedMipCount, maxMipCount));
            byte[][] encodedMips = new byte[mipCount][];
            byte[] currentData = bgraData;
            int currentWidth = width;
            int currentHeight = height;

            for (int level = 0; level < mipCount; level++)
            {
                // Cuttlefish's generateMipmaps path can access invalid memory for
                // valid full mip chains (for example 256x256 ETC2 with 9 mips).
                // Feed one independently generated level at a time so native code
                // never owns or resizes an entire mip chain.
                IntPtr image = LoadTextureFromBuffer(
                    currentData, currentData.Length, currentWidth, currentHeight, 1);
                if (image == IntPtr.Zero)
                    throw new Exception($"{nameof(LoadTextureFromBuffer)} returned null at mip {level}.");

                byte[][] converted = DoConversion(
                    image, textureFormat, quality,
                    out int convertedWidth, out int convertedHeight);
                if (converted.Length != 1)
                {
                    throw new Exception(
                        $"Native encoder returned {converted.Length} mip levels " +
                        $"while encoding mip {level}; expected exactly one.");
                }
                if (convertedWidth != currentWidth || convertedHeight != currentHeight)
                {
                    throw new Exception(
                        $"Native encoder changed mip {level} dimensions from " +
                        $"{currentWidth}x{currentHeight} to " +
                        $"{convertedWidth}x{convertedHeight}.");
                }
                encodedMips[level] = converted[0];

                if (level + 1 < mipCount)
                {
                    currentData = DownsampleBgra32Box(
                        currentData, currentWidth, currentHeight,
                        out currentWidth, out currentHeight);
                }
            }

            return encodedMips;
        }

        private static byte[] DownsampleBgra32Box(
            byte[] source, int sourceWidth, int sourceHeight,
            out int targetWidth, out int targetHeight)
        {
            targetWidth = Math.Max(1, sourceWidth / 2);
            targetHeight = Math.Max(1, sourceHeight / 2);
            byte[] target = new byte[checked(targetWidth * targetHeight * 4)];

            for (int y = 0; y < targetHeight; y++)
            {
                int sourceYStart = y * sourceHeight / targetHeight;
                int sourceYEnd = Math.Max(
                    sourceYStart + 1, (y + 1) * sourceHeight / targetHeight);
                for (int x = 0; x < targetWidth; x++)
                {
                    int sourceXStart = x * sourceWidth / targetWidth;
                    int sourceXEnd = Math.Max(
                        sourceXStart + 1, (x + 1) * sourceWidth / targetWidth);
                    int sampleCount = (sourceXEnd - sourceXStart) *
                                      (sourceYEnd - sourceYStart);
                    int targetOffset = (y * targetWidth + x) * 4;

                    for (int channel = 0; channel < 4; channel++)
                    {
                        int sum = 0;
                        for (int sourceY = sourceYStart; sourceY < sourceYEnd; sourceY++)
                        {
                            for (int sourceX = sourceXStart; sourceX < sourceXEnd; sourceX++)
                            {
                                int sourceOffset =
                                    (sourceY * sourceWidth + sourceX) * 4;
                                sum += source[sourceOffset + channel];
                            }
                        }
                        target[targetOffset + channel] =
                            (byte)((sum + sampleCount / 2) / sampleCount);
                    }
                }
            }

            return target;
        }

        public static bool NativeLibrariesSupported()
        {
            try
            {
                int sanityCheck = SanityCheck(123);
                return sanityCheck == 246;
            }
            catch
            {
                return false;
            }
        }

        private static byte[][] DoConversion(
            IntPtr image, TextureFormat textureFormat, int quality,
            out int width, out int height)
        {
            TextureDataBuffer dataBuffer = ConvertAndFreeTexture(image, textureFormat, quality);
            ThrowIfConvertFailed(ref dataBuffer);

            width = dataBuffer.Width;
            height = dataBuffer.Height;

            return ProcessTextureDataBuffer(ref dataBuffer);
        }

        private static byte[] DoConversionFlat(
            IntPtr image, TextureFormat textureFormat, int quality,
            out int width, out int height)
        {
            TextureDataBuffer dataBuffer = ConvertAndFreeTexture(image, textureFormat, quality);
            ThrowIfConvertFailed(ref dataBuffer);

            width = dataBuffer.Width;
            height = dataBuffer.Height;

            return ProcessTextureDataBufferFlat(ref dataBuffer);
        }

        private static void ThrowIfConvertFailed(ref TextureDataBuffer dataBuf)
        {
            int width = dataBuf.Width;
            int height = dataBuf.Height;
            if (height == -1)
            {
                throw width switch
                {
                    -1 => new Exception($"{nameof(ConvertAndFreeTexture)} failed to encode texture."),
                    -2 => new Exception($"{nameof(ConvertAndFreeTexture)} failed to encode mipmaps."),
                    -3 => new Exception($"{nameof(ConvertAndFreeTexture)} failed to allocate memory."),
                    _ => new Exception($"{nameof(ConvertAndFreeTexture)} returned an unknown error.")
                };
            }
        }

        private static byte[][] ProcessTextureDataBuffer(ref TextureDataBuffer dataBuf)
        {
            IntPtr mipsPtr = dataBuf.Mips;
            int mipCount = dataBuf.MipCount;

            byte[][] datas = new byte[mipCount][];
            for (int i = 0; i < mipCount; i++)
            {
                IntPtr thisMipPtr = IntPtr.Add(mipsPtr, i * Marshal.SizeOf<TextureDataMip>());
                var mip = Marshal.PtrToStructure<TextureDataMip>(thisMipPtr);
                byte[] data = new byte[mip.Size];
                Marshal.Copy(mip.Data, data, 0, mip.Size);
                datas[i] = data;
            }

            FreeTextureDataBuffer(mipsPtr, mipCount);
            return datas;
        }

        private static byte[] ProcessTextureDataBufferFlat(ref TextureDataBuffer dataBuf)
        {
            IntPtr mipsPtr = dataBuf.Mips;
            int mipCount = dataBuf.MipCount;

            int dataSize = 0;
            for (int i = 0; i < mipCount; i++)
            {
                IntPtr thisMipPtr = IntPtr.Add(mipsPtr, i * Marshal.SizeOf<TextureDataMip>());
                var mip = Marshal.PtrToStructure<TextureDataMip>(thisMipPtr);
                dataSize += mip.Size;
            }

            byte[] data = new byte[dataSize];
            int dataPtr = 0;
            for (int i = 0; i < mipCount; i++)
            {
                IntPtr thisMipPtr = IntPtr.Add(mipsPtr, i * Marshal.SizeOf<TextureDataMip>());
                var mip = Marshal.PtrToStructure<TextureDataMip>(thisMipPtr);
                Marshal.Copy(mip.Data, data, dataPtr, mip.Size);
                dataPtr += mip.Size;
            }

            FreeTextureDataBuffer(mipsPtr, mipCount);
            return data;
        }

        private static bool IsPvrtlFormat(TextureFormat format)
        {
            return format == TextureFormat.PVRTC_RGB2 ||
                   format == TextureFormat.PVRTC_RGBA2 ||
                   format == TextureFormat.PVRTC_RGB4 ||
                   format == TextureFormat.PVRTC_RGBA4;
        }

        private static bool PvrtlStubInUse()
        {
            // the pvrtexlib stub is used so we don't ship the real library (which has non-foss licensing)
            // if real pvrtexlib is being used, this will return the regular RGBA8888 bpp
            return PVRTexLib_GetFormatBitsPerPixel(RGBA8888) == 12345;
        }
    }
}
