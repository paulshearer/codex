using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Web.Script.Serialization;

// Console-only protocol fixture. It never creates windows or sends input.
internal static class DesktopAgentProtocolFixture {
    private static int Main() {
        var json = new JavaScriptSerializer();
        var input = json.Deserialize<Dictionary<string, object>>(Console.In.ReadToEnd());
        string operation = Convert.ToString(input["operation"]);
        if (operation == "hang" || operation == "hang_after_dispatch" || operation == "action") {
            if (operation == "hang_after_dispatch" || operation == "action") {
                Console.Error.WriteLine("{\"phase\":\"dispatching\",\"method\":\"fixture\"}");
                Console.Error.Flush();
            }
            Thread.Sleep(60000);
        }
        if (operation == "malformed_after_dispatch") {
            Console.Error.WriteLine("{\"phase\":\"dispatching\",\"method\":\"fixture\"}");
            Console.Write("incomplete{"); return 1;
        }
        Console.WriteLine(json.Serialize(new { ok = true, outcome = "observed", method = "fixture", pid = Process.GetCurrentProcess().Id }));
        return 0;
    }
}
