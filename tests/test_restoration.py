from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from app import LocalServer, load_project, save_project, build_export, decode_scan
from desktop import DesktopAPI
from rendering import render_photo
from restoration import defaults, process, validate_restoration
from scanner import Scan, Photo, order_quad, validate_photo


def fixture():
    image = np.full((240,220,3),246,np.uint8)
    image[20:170,20:200]=(75,115,145)
    image[195:200,60:65]=15
    image[85:89,95:99]=245
    scan=Scan(image,"restoration.png",(300,300))
    photo=Photo(order_quad([[0,0],[219,0],[219,239],[0,239]]),order_quad([[20,20],[199,20],[199,169],[20,169]]))
    return scan,photo


class RestorationTests(unittest.TestCase):
    def test_photo_layout_override_is_local_to_that_photo(self):
        scan,first=fixture();_,second=fixture()
        first.presentation={"trim":3,"occupancy":.5}
        a=render_photo(scan,first);b=render_photo(scan,second)
        self.assertEqual(a.images["paper"].shape[1],b.images["paper"].shape[1]-6)
        self.assertEqual(a.images["paper"].shape[0],b.images["paper"].shape[0]-6)
        self.assertEqual(second.presentation,{})
    def test_coloured_small_subject_details_are_not_dust(self):
        scan,photo=fixture()
        scan.image[80:86,90:96]=(80,235,95)
        photo.restoration={**defaults(),"enabled":True,"auto_border":False,"auto_image":True}
        rendered=render_photo(scan,photo)
        self.assertFalse(rendered.mask[80:86,90:96].any())

    def test_texture_guard_protects_patterned_picture(self):
        scan,photo=fixture()
        rng=np.random.default_rng(41)
        texture=rng.integers(40,215,(150,180),dtype=np.uint8)
        scan.image[20:170,20:200]=texture[:,:,None]
        photo.restoration={**defaults(),"enabled":True,"auto_border":False,"auto_image":True}
        rendered=render_photo(scan,photo)
        self.assertLess(np.count_nonzero(rendered.mask),150*180*.01)

    def test_disabled_is_pixel_identical(self):
        scan,photo=fixture()
        rendered=render_photo(scan,photo)
        for mode in ("paper","image","composition"):
            np.testing.assert_array_equal(np.array(rendered.images[mode]),np.array(rendered.original[mode]))
        self.assertEqual(rendered.stats["masked_pixels"],0)

    def test_border_and_image_automatic_regions_are_independent(self):
        scan,photo=fixture()
        original=scan.image.copy()
        photo.restoration={**defaults(),"enabled":True,"sensitivity":60}
        border=render_photo(scan,photo)
        self.assertGreater(border.images["paper"][197,62,0],200)
        np.testing.assert_array_equal(border.images["paper"][85:89,95:99],original[85:89,95:99])
        self.assertFalse(border.mask[20:170,20:200].any())
        photo.restoration.update(auto_border=False,auto_image=True)
        content=render_photo(scan,photo)
        self.assertLess(content.images["paper"][87,97,0],120)
        np.testing.assert_array_equal(content.images["paper"][195:200,60:65],original[195:200,60:65])
        np.testing.assert_array_equal(scan.image,original)

    def test_paint_erase_and_rotation_anchor_to_source(self):
        scan,photo=fixture()
        photo.restoration={**defaults(),"enabled":True,"auto_border":False,"strokes":[{"points":[[97,87]],"radius":6,"mode":"paint"}]}
        for rotation in range(4):
            photo.rotation=rotation
            rendered=render_photo(scan,photo)
            point=cv2.perspectiveTransform(np.array([[[97,87]]],np.float32),rendered.source_to_paper)[0,0]
            x,y=np.rint(point).astype(int)
            self.assertGreater(rendered.mask[y,x],0)
            self.assertLess(rendered.images["paper"][y,x,0],120)
            # The repair reaches the matching internal-image pixels after rotation.
            delta=rendered.images["image"].astype(int)-rendered.original["image"].astype(int)
            self.assertLess(delta.min(),-100)
        photo.restoration["strokes"].append({"points":[[97,87]],"radius":10,"mode":"erase"})
        excluded=render_photo(scan,photo)
        self.assertEqual(np.count_nonzero(excluded.mask),0)
        np.testing.assert_array_equal(excluded.images["paper"],excluded.original["paper"])

    def test_adjustments_do_not_alter_real_paper(self):
        scan,photo=fixture()
        photo.restoration={**defaults(),"enabled":True,"auto_border":False,"adjustments":True,"brightness":25,"contrast":10,"saturation":20}
        rendered=render_photo(scan,photo)
        np.testing.assert_array_equal(rendered.images["paper"][200:235],rendered.original["paper"][200:235])
        self.assertGreater(rendered.images["image"][50,50].mean(),rendered.original["image"][50,50].mean())

    def test_mask_is_exact_modified_area_for_inpainting(self):
        scan,photo=fixture()
        photo.restoration={**defaults(),"enabled":True,"sensitivity":60,"auto_image":True}
        rendered=render_photo(scan,photo)
        np.testing.assert_array_equal(rendered.images["paper"][rendered.mask==0],rendered.original["paper"][rendered.mask==0])
        self.assertLess(np.count_nonzero(rendered.mask),scan.image.shape[0]*scan.image.shape[1]*.02)

    def test_project_roundtrip_preserves_source_and_strokes(self):
        scan,photo=fixture()
        source=BytesIO();Image.fromarray(scan.image).save(source,"PNG",dpi=scan.dpi)
        scan,_=decode_scan(source.getvalue(),scan.name)
        server=LocalServer(("127.0.0.1",0))
        try:
            job=server.store(scan,source.getvalue())
            photo.restoration={**defaults(),"enabled":True,"auto_image":True,"strokes":[{"points":[[97,87]],"radius":5,"mode":"erase"}]}
            payload={"job":job,"photos":[photo.as_dict(scan.dpi)],"trim":2,"occupancy":.65}
            project=save_project(server,payload)
            loaded=load_project(server,project)
            self.assertEqual(loaded["photos"][0]["restoration"]["strokes"],photo.restoration["strokes"])
            self.assertEqual(loaded["trim"],2);self.assertEqual(loaded["occupancy"],.65)
            restored,raw=server.load(loaded["job"])
            np.testing.assert_array_equal(restored.image,scan.image)
            self.assertEqual(raw,source.getvalue())
            first=build_export(scan,[validate_photo(payload["photos"][0],scan.image.shape)],.65,2)
            second=build_export(restored,[validate_photo(loaded["photos"][0],restored.image.shape)],.65,2)
            with zipfile.ZipFile(BytesIO(first)) as a,zipfile.ZipFile(BytesIO(second)) as b:
                for name in a.namelist():self.assertEqual(a.read(name),b.read(name))
        finally:server.server_close()

    def test_native_save_bridge_uses_chosen_path_and_preserves_original(self):
        scan,photo=fixture()
        source=BytesIO();Image.fromarray(scan.image).save(source,"PNG")
        server=LocalServer(("127.0.0.1",0))
        Path(".tmp").mkdir(exist_ok=True)
        try:
            job=server.store(scan,source.getvalue())
            photo.restoration={**defaults(),"enabled":True,"sensitivity":60}
            with tempfile.TemporaryDirectory(dir=Path.cwd()/".tmp") as temporary:
                path=Path(temporary)/"chosen.zip"
                class Window:
                    def get_current_url(self):return f"http://127.0.0.1:{server.server_port}/"
                    def create_file_dialog(self,*args,**kwargs):return (str(path),)
                api=DesktopAPI(server);api._window=Window()
                saved=api.save_artifact("export",{"job":job,"photos":[photo.as_dict(scan.dpi)]})
                self.assertFalse(saved.get("error"),saved)
                with zipfile.ZipFile(path) as z:
                    self.assertIn("original/paper/001.png",z.namelist())
                    self.assertIn("masks/001.png",z.namelist())
                with patch.object(api._window,"create_file_dialog",return_value=None):
                    self.assertTrue(api.save_artifact("export",{"job":job})["cancelled"])
        finally:server.server_close()

    def test_invalid_restoration_and_project_rejected(self):
        for options in [{"sensitivity":float("nan")},{"radius":100},{"enabled":"yes"},{"strokes":[{"points":[[1,2]],"radius":400}]}]:
            with self.assertRaises(ValueError):validate_restoration(options)
        scan,photo=fixture()
        with self.assertRaises(ValueError):validate_photo({**photo.as_dict(),"restoration":{"strokes":[{"points":[[999,999]],"radius":5}]}},scan.image.shape)
        server=LocalServer(("127.0.0.1",0))
        try:
            with self.assertRaises(ValueError):load_project(server,b"not a project")
            stream=BytesIO()
            with zipfile.ZipFile(stream,"w") as archive:archive.writestr("../../file","bad")
            with self.assertRaises(ValueError):load_project(server,stream.getvalue())
        finally:server.server_close()


if __name__=="__main__":unittest.main()
