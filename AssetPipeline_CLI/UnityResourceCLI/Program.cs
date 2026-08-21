using System;

namespace UnityResourceCLI
{
    internal static class Program
    {
        public static int Main(string[] args)
        {
            try
            {
                if (args.Length == 0)
                {
                    CliOptions.PrintHelp();
                    return 0;
                }

                if (string.Equals(args[0], "encode-texture-worker", StringComparison.OrdinalIgnoreCase))
                    return NativeTextureWorker.Run(args[1..]);

                CliOptions options = CliOptions.Parse(args);
                if (options.ShowHelp)
                {
                    CliOptions.PrintHelp();
                    return 0;
                }

                if (options.Command == "verify")
                    return new ResourceVerifier(options).Run();

                return new ResourcePipeline(options).Run();
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.Message);
                Console.Error.WriteLine(ex);
                return 1;
            }
        }
    }
}
