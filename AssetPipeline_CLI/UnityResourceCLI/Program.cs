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

                CliOptions options = CliOptions.Parse(args);
                if (options.ShowHelp)
                {
                    CliOptions.PrintHelp();
                    return 0;
                }

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
