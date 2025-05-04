from lightkurve import TessLightCurveFile, TessTargetPixelFile
import matplotlib.pyplot as plt
from json import loads, dumps
from os import makedirs, chdir, system, getcwd
from gc import collect
from tkinter import Tk, Label
from PIL import Image, ImageTk


class Project():
    def __init__(self):
        pass
    
    def create(self, name):
        self.name = name
        makedirs(name)
        chdir(name)
        self.cpath = getcwd()
        self.setup()
        print(f"Successfully created {name}!!")
    
    def restore(self, name):
        self.name = name
        chdir(name)
        self.cpath = getcwd()
        self.config_file = open(r"config.json", "r")
        self.config = loads(self.config_file.read())
        self.config_file.close()
        print(f"Successfully restored {name}!!")

    def setup(self):
        makedirs(r"LightCurves")
        makedirs(r"TargetPixels")
        makedirs(r"Graphs")
        self.dwprogress = {"Type":None, "Script": None, "ToDownload": None, "Downloaded": 0, "List": []}
        self.pcprogress = {"ToProcess": None, "Processed": 0, "List": []}
        self.fnprogress = {"ToFinalise": None, "Finalised": 0, "List": []}
        self.config_file = open(r"config.json", "w")
        self.config = {
            "Name": self.name, 
            "Download_Progress": self.dwprogress, 
            "Processing_Progress": self.pcprogress, 
            "Finalising_Progress": self.fnprogress
                       }
        self.config_file.write(dumps(self.config))
        self.config_file.close()

    def updateconfig(self):
        chdir(self.cpath)
        self.config_file = open("config.json", "w")
        self.config_file.write(dumps(self.config))
        self.config_file.close()

    def download(self, type, script_path, amount):
        self.config["Download_Progress"]["Script"] = script_path
        self.config["Download_Progress"]["Type"] = type
        self.config["Download_Progress"]["ToDownload"] = amount
        self.updateconfig()
        script = open(script_path, "r")
        lnum = 0
        chdir(self.cpath)
        if type=="lc":
            chdir(r"LightCurves")
        elif type=="tp":
            chdir(r"TargetPixels")
        else:
            raise TypeError
        commands = []
        for x in range(amount):
            commands.append(script.readline().strip())
        script.close()
        for x in commands:
            if lnum<self.config["Download_Progress"]["Downloaded"]:
                continue
            elif lnum>=self.config["Download_Progress"]["Downloaded"] and self.config["Download_Progress"]["Downloaded"]<=self.config["Download_Progress"]["ToDownload"]:
                system(x)
                lnum+=1
                self.config["Download_Progress"]["Downloaded"]+=1
                self.config["Download_Progress"]["List"].append(x.split(" ")[-2])
                print(f"{lnum}/{amount}")
        self.updateconfig()

            
    def process(self, amount):
        self.config["Processing_Progress"]["ToProcess"] = amount
        lnum = 0
        self.updateconfig()
        paths = []
        for x in range(amount):
            paths.append(self.config["Download_Progress"]["List"][x])
        for x in paths:
            if lnum<self.config["Processing_Progress"]["Processed"]:
                continue
            elif lnum>=self.config["Processing_Progress"]["Processed"] and self.config["Processing_Progress"]["Processed"]<=self.config["Processing_Progress"]["ToProcess"]:
                indpath = self.indprocess(x)
                lnum+=1
                self.config["Processing_Progress"]["Processed"]+=1
                self.config["Processing_Progress"]["List"].append(indpath)
                print(f"{lnum}/{amount}")
        self.updateconfig()
    

    def indprocess(self, path):
        if self.config["Download_Progress"]["Type"]=="lc":
            lc = TessLightCurveFile(fr"LightCurves\{path}")
            lc = lc.remove_nans().flatten()
            lc.plot()
            plt.savefig(getcwd()+fr"\Graphs\{path.strip('.fits')+'.jpeg'}", bbox_inches="tight")
            plt.close()
            del lc
            collect()
            return path.strip('.fits')+'.jpeg'
        elif self.config["Download_Progress"]["Type"]=="tp":
            tp = TessTargetPixelFile(fr"TargetPixels\{path}")
            tp = tp.to_lightcurve(aperture_mask="all")
            tp = tp.remove_nans().flatten()
            tp.plot()
            plt.savefig(getcwd()+fr"\Graphs\{path.strip('.fits')+'.jpeg'}", bbox_inches="tight")
            plt.close()
            del tp
            collect()
            return path.strip('.fits')+'.jpeg'


    def finalise(self, amount):
        self.config["Finalising_Progress"]["ToFinalise"] = amount
        self.updateconfig()
        lnum = 0
        imagelist = []
        for x in range(amount):
            imagelist.append(self.config["Processing_Progress"]["List"][x])
        imagelist = imagelist[self.config["Finalising_Progress"]["Finalised"]:self.config["Finalising_Progress"]["ToFinalise"]]
        self.indfinalise(imagelist)

    def indfinalise(self, image_paths):
        index = 0
        root = Tk()
        root.title("Image Viewer")
        label = Label(root)
        label.pack()

        if not image_paths:
            pass
  
        def update_image():
            nonlocal index
            photo = ImageTk.PhotoImage(Image.open(getcwd()+fr"/Graphs/{image_paths[index]}"))
            label.config(image=photo)
            label.image = photo

        def key_handler(event):
            nonlocal index
            if event.keysym == 'Right' and index!=len(image_paths)-1:
                index = (index + 1) % len(image_paths)
                update_image()
            elif event.keysym == 'Left' and index!=0:
                index = (index - 1) % len(image_paths)
                update_image()
            elif event.keysym == 'Down':
                self.config["Finalising_Progress"]["List"].append(image_paths[index])
                print(f"Appended: {image_paths[index]}")
                self.updateconfig()
            elif event.keysym == 'Escape':
                self.config["Finalising_Progress"]["Finalised"]+=index+1
                self.updateconfig()
                root.destroy()

        root.bind("<Key>", key_handler)
        update_image()
        root.mainloop()

    def getresults(self):
        finallist = []
        for x in set(self.config["Finalising_Progress"]["List"]):
            finallist.append("TIC "+x.split("-")[2].lstrip("0"))
        return finallist
    
    def saveresults(self, name):
        fw = open(name, 'w')
        fw.write(dumps(self.getresults()))
        fw.close()
        